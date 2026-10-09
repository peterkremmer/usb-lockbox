"""Windows backend (PowerShell + raw disk I/O).

STATUS: UNTESTED ON REAL HARDWARE. Run `python -m usblockbox --selftest-windows` first (read-only),
then test destructive steps on sacrificial drives on a spare machine only. Items marked VERIFY
depend on Windows/PowerShell behavior I could not confirm from this build environment.

Design rules:
 * No bypassing of policy: PowerShell runs with default execution policy; failures are reported.
 * Passwords never go on a command line; they are passed in the child environment ($env:USBLOCKBOX_PW).
 * Raw disk writes happen only on \\\\.\\PhysicalDriveN after Clear-Disk left no mounted volumes.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from .. import diag, nativedisk, throughput, usbports
from ..junk import junk_label
from ..models import BitLockerInfo, DriveInfo, PartitionInfo, Port, VolumeInfo, clean_serial
from .base import Backend, BackendError, Cancelled, CancelCheck, Progress

CHUNK = 4 * 1024 * 1024
HW_ENCRYPTED_HINTS = ("ironkey", "aegis", "datashur", "kanguru defender", "apricorn", "securedrive")
_SKIP_DIRS = {"system volume information"}


def describe_counts(top: int, per: dict, hidden: set, mac_meta: int = 0) -> str:
    """e.g. "2 at the top level, 6375 in FOUND.000 (Windows disk-check (chkdsk) recovery folder, hidden)"."""
    parts = [f"{top} at the top level"]
    for name, c in sorted(per.items(), key=lambda kv: -kv[1])[:3]:
        if not c:
            continue
        tags = [t for t in (junk_label(name), "hidden" if name in hidden else "") if t]
        parts.append(f"{c} in {name}" + (f" ({', '.join(tags)})" if tags else ""))
    if mac_meta:
        parts.append(f"{mac_meta} Mac metadata files (._*)")
    return ", ".join(parts)


def run_ps(script: str, env_extra: Optional[dict] = None, timeout: int = 120, label: str = "") -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    label = label or diag.ps_label(script)        # the cmdlet name only: never the script text
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        why = "timed out after %s s" % timeout if isinstance(e, subprocess.TimeoutExpired) else str(e)
        diag.ps_call(label, time.perf_counter() - t0, -1, why)
        raise BackendError(f"PowerShell failed to run: {e}", "PS_LAUNCH")
    diag.ps_call(label, time.perf_counter() - t0, p.returncode, p.stderr or p.stdout)
    if p.returncode != 0:
        raise BackendError((p.stderr or p.stdout).strip() or f"PowerShell exit {p.returncode}", "PS_ERROR")
    return p.stdout


def _log_query_times(r: dict) -> None:
    """Per-drive timings reported by the listing script (milliseconds), so a slow query can be named."""
    try:
        t = str(r.get("T") or "")
        ms = [int(x) for x in re.findall(r"=(\d+)", t)]
        if not diag.startup_done():
            diag.log.info("Disk %s query times (ms): %s", r.get("Number"), t)
        elif ms and sum(ms) >= diag.SLOW_SECONDS * 1000:
            diag.log.warning("SLOW: disk %s query times (ms): %s", r.get("Number"), t)
    except Exception:   # noqa: BLE001
        pass


def pnp_id_from_disk_path(path: str) -> str:
    """The device instance id a Get-Disk Path stands for, e.g. a path like
    //?/usbstor#disk&ven_x&prod_y&rev_1#0123&0#{guid} becomes USBSTOR/DISK&VEN_X&PROD_Y&REV_1/0123&0 (with backslashes).
    Empty when the path is not in that form."""
    p = re.sub(r"^\\\\[?.]\\", "", (path or "").strip())
    p = re.sub(r"#\{[0-9a-fA-F-]+\}$", "", p)
    parts = p.split("#")
    return "\\".join(parts).upper() if len(parts) == 3 else ""


def _json(text: str):
    text = text.strip()
    return json.loads(text) if text else None


def _as_list(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


_LIST_SCRIPT = r"""
$ErrorActionPreference='Stop'
$out=@()
$only=$env:USBLOCKBOX_DISK
$disks=if($only){ @(Get-Disk -Number ([int]$only) -ErrorAction SilentlyContinue | Where-Object { $_.BusType -eq 'USB' }) } else { @(Get-Disk | Where-Object { $_.BusType -eq 'USB' }) }
foreach($d in $disks){
  $n=$d.Number
  $sw=[Diagnostics.Stopwatch]::StartNew()
  $wmi=Get-CimInstance Win32_DiskDrive -Filter "Index=$n"
  $m1=$sw.ElapsedMilliseconds
  $parts=@();$vols=@()
  if(-not $env:USBLOCKBOX_NOPARTS){
    $allp=@(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue)
    $parts=@($allp | ForEach-Object {
      [pscustomobject]@{Number=$_.PartitionNumber;Type=[string]$_.Type;Size=[int64]$_.Size;Active=[bool]$_.IsActive;Hidden=[bool]$_.IsHidden;Letter=[string]$_.DriveLetter}})
    $vols=@($allp | Where-Object {$_.DriveLetter} | ForEach-Object {
      $v=Get-Volume -DriveLetter $_.DriveLetter -ErrorAction SilentlyContinue
      [pscustomobject]@{Letter=[string]$_.DriveLetter;FS=[string]$v.FileSystem;Label=[string]$v.FileSystemLabel;Size=[int64]$v.Size;Free=[int64]$v.SizeRemaining}})
  }
  $m2=$sw.ElapsedMilliseconds
  $loc='';$vidpid='';$pnp=[string]$wmi.PNPDeviceID
  if(-not $env:USBLOCKBOX_NOPNP){
    try{
      $parent=(Get-PnpDeviceProperty -InstanceId $pnp -KeyName 'DEVPKEY_Device_Parent').Data
      $loc=((Get-PnpDeviceProperty -InstanceId $parent -KeyName 'DEVPKEY_Device_LocationPaths').Data) -join ';'
      if($parent -match 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})'){ $vidpid=$matches[1]+':'+$matches[2] }
    }catch{}
  }
  $m3=$sw.ElapsedMilliseconds
  $out+=[pscustomobject]@{
    Number=$n;UniqueId=[string]$d.UniqueId;Serial=([string]$d.SerialNumber).Trim();Model=[string]$d.FriendlyName
    Firmware=[string]$d.FirmwareVersion;Size=[int64]$d.Size;Bus=[string]$d.BusType;IsSystem=[bool]$d.IsSystem
    IsBoot=[bool]$d.IsBoot;ReadOnly=[bool]$d.IsReadOnly;Style=[string]$d.PartitionStyle
    Media=[string]$wmi.MediaType;Location=$loc;VidPid=$vidpid;PnpId=$pnp;Parts=$parts;Vols=$vols
    T=('cim={0} partitions+volumes={1} pnp={2}' -f $m1,($m2-$m1),($m3-$m2))}
}
ConvertTo-Json -InputObject @($out) -Depth 6
"""

# Who is plugged in right now: one cheap call. Everything slow (partitions, volumes, BitLocker, files) is read once per
# drive and remembered, instead of for every drive on every poll.
_QUICK_SCRIPT = r"""
$ErrorActionPreference='Stop'
$out=@()
foreach($d in @(Get-Disk | Where-Object { $_.BusType -eq 'USB' })){
  $out+=[pscustomobject]@{Number=$d.Number;UniqueId=[string]$d.UniqueId;Serial=([string]$d.SerialNumber).Trim();Size=[int64]$d.Size;Path=[string]$d.Path}
}
ConvertTo-Json -InputObject @($out) -Depth 3
"""

DETAIL_TTL = 1800         # seconds a drive's details are reused (they are also dropped the moment the drive is gone).
                          # Reading an encrypted drive locks and unlocks it to test the password, so do it rarely.
DETAIL_WORKERS = 3        # drives read at the same time
DETAIL_RETRY = 30         # seconds before a drive whose read failed is tried again

_BL_SCRIPT = r"""
$ErrorActionPreference='Stop'
try{
  $b=Get-BitLockerVolume -MountPoint '%LETTER%:'
  [pscustomobject]@{Present=($b.VolumeStatus -ne 'FullyDecrypted');Protected=($b.ProtectionStatus -eq 'On')
    Method=[string]$b.EncryptionMethod;Percent=[double]$b.EncryptionPercentage;Locked=($b.LockStatus -eq 'Locked')
    Protectors=@($b.KeyProtector | ForEach-Object {[string]$_.KeyProtectorType});Status=[string]$b.VolumeStatus} | ConvertTo-Json -Depth 4
}catch{ '{"Present":false}' }
"""


class WindowsBackend(Backend):
    name = "windows"
    real = True

    def __init__(self, password_getter=None, elevated: bool = True):
        if sys.platform != "win32":
            raise BackendError("The Windows backend only runs on Windows.", "PLATFORM")
        self._pw = password_getter or (lambda: "")
        self.elevated = elevated
        self._scanner = usbports.PortScanner(run_ps, locator=usbports.native_hub_locations)
        self.ports_note = ""

    def list_ports(self, occupied):
        ports, self.ports_note = self._scanner.scan(occupied)
        return ports

    # ------------------------------------------------------------ read-only
    def system_disk_numbers(self) -> set[int]:
        """Cached for a minute: the safety re-check asks before every step of every drive."""
        with self._sys_lock:
            now = time.monotonic()
            if self._sys_cache and now - self._sys_cache[0] < 60:
                return set(self._sys_cache[1])
            nums = self._read_system_disks()
            self._sys_cache = (now, set(nums))
            return nums

    def _read_system_disks(self) -> set[int]:
        out = run_ps(r"""
$n=@(Get-Disk | Where-Object { $_.IsSystem -or $_.IsBoot } | ForEach-Object { $_.Number })
$sd=$env:SystemDrive.Substring(0,1)
$n+=@(Get-Partition -DriveLetter $sd | ForEach-Object { $_.DiskNumber })
Get-CimInstance Win32_PageFileUsage | ForEach-Object { $l=$_.Name.Substring(0,1); $n+=@(Get-Partition -DriveLetter $l -ErrorAction SilentlyContinue | ForEach-Object { $_.DiskNumber }) }
ConvertTo-Json -InputObject @($n | Select-Object -Unique)
""", label="find system disks")
        return {int(x) for x in _as_list(_json(out))}

    _native_pnp = True              # drive location paths are read from Windows directly; turned off if that finds nothing
    _sys_cache = None               # (when, disk numbers): the system disk does not change while the app runs
    _sys_lock = threading.Lock()

    def _list_rows(self, only: Optional[int] = None) -> list[dict]:
        """Rows from the listing script. The slow per-drive PnP property lookups are skipped and done natively."""
        label = "list USB disks" if only is None else "check USB disk %d" % int(only)
        env: dict = {} if only is None else {"USBLOCKBOX_DISK": str(int(only))}
        native = self._native_pnp
        if native:
            env["USBLOCKBOX_NOPNP"] = "1"
        if nativedisk.enabled():
            env["USBLOCKBOX_NOPARTS"] = "1"
        rows = _as_list(_json(run_ps(_LIST_SCRIPT, dict(env) or None, 90, label)))
        if env.get("USBLOCKBOX_NOPARTS"):
            try:
                for r in rows:
                    r["Style"], r["Parts"], r["Vols"] = nativedisk.native_parts_and_volumes(int(r["Number"]))
            except Exception as e:   # noqa: BLE001 - PowerShell answers instead
                diag.log.warning("Native partition read failed (%s: %s); using PowerShell for it", type(e).__name__, e)
                env.pop("USBLOCKBOX_NOPARTS", None)
                rows = _as_list(_json(run_ps(_LIST_SCRIPT, dict(env) or None, 90, label)))
        for r in rows:
            _log_query_times(r)
        if native and rows:
            found = 0
            for r in rows:
                try:
                    r["Location"], r["VidPid"] = usbports.native_disk_location(str(r.get("PnpId") or ""))
                    found += 1 if r["Location"] else 0
                except Exception as e:   # noqa: BLE001
                    diag.log.info("No native location for disk %s: %s", r.get("Number"), e)
            if not found:
                diag.log.warning("Native location lookup found nothing for %d drive(s); using PowerShell for it from now on",
                                 len(rows))
                self._native_pnp = False
                env.pop("USBLOCKBOX_NOPNP", None)
                rows = _as_list(_json(run_ps(_LIST_SCRIPT, dict(env) or None, 90, label)))
        return rows

    streams_drives = True           # list_usb_disks can report drives as they become ready (on_ready)
    _detail_lock = threading.Lock()

    def _store(self) -> tuple[dict, dict]:
        d = self.__dict__
        return d.setdefault("_detail", {}), d.setdefault("_detail_fail", {})

    _unreadable: list = []
    _busy: set = set()               # disks being processed right now: the listing must not touch them

    def hold(self, disk_number: int) -> None:
        """Called when processing starts on a disk. While held, the listing neither re-reads nor locks/unlocks it."""
        with self._detail_lock:
            self.__dict__["_busy"] = set(self._busy) | {int(disk_number)}
        self._throughput().ensure_running()

    def _throughput(self) -> "throughput.ThroughputMonitor":
        mon = self.__dict__.get("_tp")
        if mon is None:
            mon = self.__dict__["_tp"] = throughput.ThroughputMonitor(run_ps, lambda: set(self._busy), self._describe_for_log)
        return mon

    def _describe_for_log(self, disk_number: int) -> Optional[dict]:
        """Serial and USB link chain of a disk being processed, from what was read about it earlier."""
        with self._detail_lock:
            entry = self._store()[0].get(int(disk_number))
        if not entry:
            return None
        d = entry[1]
        return {"label": "serial %s" % d.serial, "chain": self._scanner.link_chain(d.location_path) if d.location_path else []}

    def release(self, disk_number: int) -> None:
        with self._detail_lock:
            self.__dict__["_busy"] = set(self._busy) - {int(disk_number)}
        mon = self.__dict__.get("_tp")
        if mon is not None:
            mon.poke()

    def unreadable_drives(self) -> list[tuple[str, str]]:
        return list(self._unreadable)

    def invalidate_cache(self) -> None:
        """Forget what was read about each drive, so the next scan reads every drive again."""
        with self._detail_lock:
            for store in self._store():
                store.clear()

    def _read_detail(self, number: int) -> Optional[DriveInfo]:
        for r in self._list_rows(number):
            if int(r["Number"]) == number:
                return self._to_drive(r)
        return None                  # unplugged while we were reading

    def list_usb_disks(self, on_ready=None) -> list[DriveInfo]:
        """The USB drives plugged in now. One quick call says who is there; each drive's details are read once (a few
        drives at a time), remembered while the same drive stays in, and reported through on_ready as they arrive."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        quick = _as_list(_json(run_ps(_QUICK_SCRIPT, None, 90, "list USB disk identities")))
        wanted = {int(r["Number"]): (int(r["Number"]), str(r.get("UniqueId", "")), str(r.get("Serial", "")),
                                     int(r.get("Size") or 0)) for r in quick}
        pnp = {int(r["Number"]): pnp_id_from_disk_path(str(r.get("Path") or "")) for r in quick}
        now = time.monotonic()
        with self._detail_lock:
            cache, failed = self._store()
            busy = set(self._busy) & set(wanted)
            for n in list(cache):
                if n in busy:
                    continue                                      # being processed: keep what we know, read nothing
                if n not in wanted or cache[n][0] != wanted[n] or now - cache[n][2] > DETAIL_TTL:
                    del cache[n]                                  # gone, replaced by another drive, or too old
            for n in list(failed):
                if n not in wanted:
                    del failed[n]
            ready = {n: cache[n][1] for n in wanted if n in cache}
            todo = [n for n in wanted if n not in ready and n not in busy and now - failed.get(n, -1e9) >= DETAIL_RETRY]

        def report() -> None:
            if on_ready:
                try:
                    on_ready(sorted(ready.values(), key=lambda d: d.disk_number))
                except Exception as e:   # noqa: BLE001
                    diag.log.warning("Reporting drives failed: %s", e)

        if ready:
            report()
        if todo:
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(todo))) as pool:
                futures = {pool.submit(self._read_detail, n): n for n in todo}
                for f in as_completed(futures):
                    n = futures[f]
                    try:
                        d = f.result()
                    except Exception as e:   # noqa: BLE001 - one drive that will not answer must not hide the others
                        diag.log.warning("Could not read disk %d (%s: %s); trying again in %d s", n, type(e).__name__,
                                         e, DETAIL_RETRY)
                        with self._detail_lock:
                            self._store()[1][n] = time.monotonic()
                        continue
                    if d is None:
                        continue
                    with self._detail_lock:
                        cache, failed = self._store()
                        cache[n] = (wanted[n], d, time.monotonic())
                        failed.pop(n, None)
                    ready[n] = d
                    report()
            diag.note("read details of %d drive(s), %d at a time" % (len(todo), DETAIL_WORKERS), time.perf_counter() - t0)
        self._unreadable = self._find_unreadable(wanted, ready, pnp)
        return sorted(ready.values(), key=lambda d: d.disk_number)

    def _find_unreadable(self, wanted: dict, ready: dict, pnp: dict) -> list[tuple[str, str]]:
        """Plugged in, but its details could not be read (yet): where it is, from Windows' own device tree."""
        out = []
        with self._detail_lock:
            failed = set(self._store()[1])
        for n in wanted:
            if n in ready or n not in failed or not pnp.get(n):
                continue
            try:
                loc = usbports.native_disk_location(pnp[n])[0]
            except Exception as e:   # noqa: BLE001
                diag.log.info("No location for unreadable disk %d: %s", n, e)
                continue
            if loc:
                out.append((loc, "Windows is not answering for this drive"))
        return out

    _get_drive_lock = threading.Lock()

    def get_drive(self, disk_number: int) -> Optional[DriveInfo]:
        """One drive, read afresh. The safety re-check runs before every destructive step of every drive,
        so this must not enumerate all USB disks (with many drives on a slow PC that took longer than the
        timeout) and must not run for several drives at once."""
        with self._get_drive_lock:
            for r in self._list_rows(int(disk_number)):
                d = self._to_drive(r)
                if d.disk_number == disk_number:
                    return d
        return None

    def _to_drive(self, r: dict) -> DriveInfo:
        model = r.get("Model") or ""
        d = DriveInfo(
            disk_number=int(r["Number"]), unique_id=r.get("UniqueId", ""), serial=clean_serial(r.get("Serial", "")),
            vid_pid=r.get("VidPid", ""), model=model, firmware=r.get("Firmware", ""),
            size_bytes=int(r.get("Size", 0)), bus_type=r.get("Bus", ""),
            is_removable=("removable" in (r.get("Media") or "").lower()),   # VERIFY on your drive models
            is_system=bool(r.get("IsSystem")), is_boot=bool(r.get("IsBoot")),
            is_read_only=bool(r.get("ReadOnly")), location_path=usbports.canonical_path(r.get("Location", "")),
            partition_style=r.get("Style", ""))
        d.hardware_encrypted_suspected = any(h in model.lower() for h in HW_ENCRYPTED_HINTS)  # VERIFY
        speed = self._scanner.port_speed(d.location_path) if d.location_path else None
        d.link_speed = -1 if speed is None else int(speed)
        for p in _as_list(r.get("Parts")):
            d.partitions.append(PartitionInfo(int(p["Number"]), str(p["Type"]), int(p["Size"]),
                                              bool(p["Active"]), bool(p["Hidden"])))
        for v in _as_list(r.get("Vols")):
            d.volumes.append(VolumeInfo(v["Letter"], v["FS"], v["Label"], int(v["Size"] or 0),
                                        int((v["Size"] or 0) - (v["Free"] or 0)), None))
        with diag.timed("read boot sector of disk %d" % d.disk_number):
            d.mbr_boot_code_present = self._boot_code_present(d.disk_number)
        with diag.timed("read BitLocker state and count files on disk %d" % d.disk_number):
            self._fill_bitlocker_and_files(d)
        return d

    def _boot_code_present(self, n: int) -> bool:
        try:
            with open(rf"\\.\PhysicalDrive{n}", "rb", buffering=0) as f:
                sector = f.read(512)
            return any(sector[:440])        # boot code area; GPT protective MBR leaves it zero
        except OSError:
            return False

    def _fill_bitlocker_and_files(self, d: DriveInfo) -> None:
        for v in d.volumes:
            if not v.drive_letter:
                continue
            info = _json(run_ps(_BL_SCRIPT.replace("%LETTER%", v.drive_letter))) or {}
            if info.get("Present"):
                d.bitlocker = BitLockerInfo(True, bool(info.get("Protected")), info.get("Method", ""),
                                            float(info.get("Percent", 0)), bool(info.get("Locked")),
                                            _as_list(info.get("Protectors")), False)
                d.bitlocker.fully_encrypted = info.get("Status") == "FullyEncrypted"
                # Reading is read-only: it never locks or unlocks anything. Whether the fixed password opens the drive is
                # tested separately, once, when a drive is first checked (prove_fixed_password).
            if not (d.bitlocker.present and d.bitlocker.locked):
                t0 = time.perf_counter()
                v.file_count, v.file_detail = self._count_files(v.drive_letter)
                diag.note("count files on %s: (%s files)" % (v.drive_letter, v.file_count), time.perf_counter() - t0)

    def link_chain(self, drive: DriveInfo) -> list:
        return self._scanner.link_chain(drive.location_path) if drive.location_path else []

    def prove_fixed_password(self, drive: DriveInfo, password: str) -> None:
        """Does the fixed password open this already-encrypted drive? The only way to know is to lock the volume and unlock
        it with the password, which interrupts any encryption still running, so it is done only for a drive that Windows
        reports as FULLY encrypted, that is not being processed, and not from the listing: once, when the drive is first
        checked. A locked drive that the password opens is counted for files afterwards, as before."""
        bl = drive.bitlocker
        if not (password and bl.present and bl.fully_encrypted and bl.percent_encrypted >= 100):
            return
        if drive.disk_number in self._busy:
            return
        was_locked = bl.locked
        ok = self.verify_password(drive, password)
        bl.unlocked_with_fixed_password = ok
        if ok:
            bl.locked = False
            if was_locked:
                for v in drive.volumes:
                    if v.drive_letter and v.file_count is None:
                        v.file_count, v.file_detail = self._count_files(v.drive_letter)

    @staticmethod
    def _count_files(letter: str, cap: int = 200_000) -> tuple[Optional[int], str]:
        """Count every file (hidden and system files too, since erasing removes them all) and say where they are."""
        base = f"{letter}:\\"
        n = top = mac_meta = 0
        per: dict[str, int] = {}
        hidden: set[str] = set()
        try:
            for root, dirs, files in os.walk(base):
                dirs[:] = [x for x in dirs if x.lower() not in _SKIP_DIRS]
                n += len(files)
                mac_meta += sum(1 for f in files if f.startswith("._"))
                rel = os.path.relpath(root, base)
                if rel == ".":
                    top += len(files)
                    for dname in dirs:       # remember which top-level folders are hidden/system
                        try:
                            if os.stat(os.path.join(base, dname)).st_file_attributes & 0x6:
                                hidden.add(dname)
                        except (OSError, AttributeError):
                            pass
                else:
                    name = rel.split(os.sep)[0]
                    per[name] = per.get(name, 0) + len(files)
                if n >= cap:
                    break
        except OSError:
            return None, ""
        return n, describe_counts(top, per, hidden, mac_meta)

    def verify_password(self, drive: DriveInfo, password: str) -> bool:
        """Non-destructive: lock then unlock with the password (VERIFY that Lock on an unlocked
        removable volume is acceptable in your environment)."""
        for v in drive.volumes:
            if not v.drive_letter:
                continue
            try:
                run_ps(f"Lock-BitLocker -MountPoint '{v.drive_letter}:' -ForceDismount | Out-Null; "
                       f"Unlock-BitLocker -MountPoint '{v.drive_letter}:' "
                       f"-Password (ConvertTo-SecureString $env:USBLOCKBOX_PW -AsPlainText -Force) | Out-Null",
                       {"USBLOCKBOX_PW": password})
                return True
            except BackendError:
                return False
        return False

    # ------------------------------------------------------------ destructive
    def clear_disk(self, drive):
        n = int(drive.disk_number)
        run_ps(f"Clear-Disk -Number {n} -RemoveData -RemoveOEM -Confirm:$false", timeout=180)

    def _raw_write(self, drive, offset: int, length: int, pattern) -> None:
        with open(rf"\\.\PhysicalDrive{drive.disk_number}", "r+b", buffering=0) as f:
            f.seek(offset)
            left = length
            while left > 0:
                n = min(CHUNK, left)
                f.write(pattern(n))
                left -= n

    def zero_edges(self, drive, mb):
        size = drive.size_bytes
        length = mb * 1024 * 1024
        try:
            self._raw_write(drive, 0, length, lambda n: bytes(n))
            self._raw_write(drive, size - length, length, lambda n: bytes(n))
        except OSError as e:
            raise BackendError(f"Raw write failed: {e}", "RAW_WRITE")

    def overwrite(self, drive, passes, verify, progress, cancelled):
        size = (drive.size_bytes // 4096) * 4096
        patterns = []
        for p in range(passes):
            last = p == passes - 1
            patterns.append((lambda n: bytes(n)) if last else
                            ((lambda n: b"\xff" * n) if p % 2 == 0 else (lambda n: os.urandom(n))))
        try:
            with open(rf"\\.\PhysicalDrive{drive.disk_number}", "r+b", buffering=0) as f:
                for p, pat in enumerate(patterns):
                    f.seek(0)
                    done = 0
                    while done < size:
                        if cancelled():
                            raise Cancelled("Cancelled by operator.")
                        n = min(CHUNK, size - done)
                        f.write(pat(n))
                        done += n
                        progress((p + done / size) / passes)
                if verify:       # final pass is zeros: sample-read and compare
                    for _ in range(64):
                        off = random.randrange(0, max(size - CHUNK, 1), 4096)
                        f.seek(off)
                        if any(f.read(4096)):
                            raise BackendError("Read-back verification failed (non-zero data after final pass).", "VERIFY")
        except OSError as e:
            raise BackendError(f"Raw write failed: {e}", "RAW_WRITE")

    def _diskpart(self, script: str, timeout: int = 120) -> str:
        """Run a diskpart script (only ever built from an integer disk number and fixed words)."""
        import tempfile
        fd, path = tempfile.mkstemp(prefix="usblockbox_dp_", suffix=".txt")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(script)
            try:
                p = subprocess.run(["diskpart.exe", "/s", path], capture_output=True, text=True, timeout=timeout,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired) as e:
                raise BackendError(f"diskpart failed to run: {e}", "DISKPART")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if p.returncode != 0:
            raise BackendError("diskpart failed: " + ((p.stdout or "") + (p.stderr or "")).strip()[-600:], "DISKPART")
        return p.stdout

    # Windows' Shell Hardware Detection service is what pops up "You need to format the disk in drive X:" when a
    # new, still-unformatted volume gets a letter. It is paused while we partition and format, and always restarted.
    _hw_lock = threading.Lock()
    _hw_users = 0
    _hw_stopped = False

    @classmethod
    def _hw_restart(cls) -> None:
        with cls._hw_lock:
            if cls._hw_stopped:
                try:
                    run_ps("Start-Service ShellHWDetection", timeout=60)
                except BackendError:
                    pass
                cls._hw_stopped = False

    @contextlib.contextmanager
    def _shell_hw_paused(self):
        cls = type(self)
        with cls._hw_lock:
            cls._hw_users += 1
            if cls._hw_users == 1:
                try:
                    out = run_ps("$s=Get-Service ShellHWDetection -ErrorAction SilentlyContinue; "
                                 "if ($s -and $s.Status -eq 'Running') { Stop-Service ShellHWDetection -Force "
                                 "-ErrorAction Stop; 'stopped' } else { 'skip' }", timeout=60)
                    cls._hw_stopped = out.strip().endswith("stopped")
                except BackendError:
                    cls._hw_stopped = False            # could not pause it: carry on, the prompt is only a nuisance
                if cls._hw_stopped:
                    atexit.register(cls._hw_restart)    # even if the app is closed mid-run
        try:
            yield
        finally:
            with cls._hw_lock:
                cls._hw_users -= 1
                last = cls._hw_users == 0
            if last:
                cls._hw_restart()

    # diskpart talks to the one Virtual Disk Service. Two drives cleaned at the same moment can leave one of
    # them "not convertible" (seen with three drives started together), so this step runs one drive at a time.
    _dp_lock = threading.Lock()

    def _clean_and_convert(self, n: int, style: str) -> None:
        script = f"select disk {n}\nclean\nconvert {style.lower()}\n"
        with self._dp_lock:
            try:
                self._diskpart(script)
            except BackendError as e:
                if "not convertible" not in str(e).lower() and "not convertable" not in str(e).lower():
                    raise
                diag.log.warning("diskpart said disk %s is not convertible; refreshing and retrying once", n)
                try:
                    run_ps("Update-HostStorageCache", timeout=60)
                except Exception:
                    pass
                time.sleep(3)
                self._diskpart(script)

    def init_partition_format(self, drive, style, fs, label):
        """Partition, format, and only then give the volume a letter, stopping at the first error.

        Why diskpart for clean/convert: after a wipe, Windows reports a blank USB flash drive as MBR with one
        whole-disk partition ("superfloppy"), so Initialize-Disk says "already initialized" and New-Partition says
        "Not enough available capacity". diskpart clean + convert works (verified by hand on one flash drive,
        2026-10-02). Why the letter comes last: a lettered, unformatted volume makes Explorer offer to format it."""
        n = int(drive.disk_number)
        style = str(style).upper()
        if style not in ("GPT", "MBR"):
            raise BackendError(f"Unsupported partition style {style!r}.", "BAD_STYLE")
        safe_label = re.sub(r"[^A-Za-z0-9_ -]", "", label)[:11]
        with self._shell_hw_paused():
            self._clean_and_convert(n, style)
            out = run_ps(
                f"$ErrorActionPreference='Stop'; Update-HostStorageCache; "
                f"$d=Get-Disk -Number {n}; "
                f"if ($d.PartitionStyle -ne '{style}') {{ throw \"The disk is $($d.PartitionStyle), expected {style}.\" }}; "
                f"$p=New-Partition -DiskNumber {n} -UseMaximumSize; "
                f"Format-Volume -Partition $p -FileSystem {fs} -NewFileSystemLabel '{safe_label}' -Confirm:$false | Out-Null; "
                f"$p=Get-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber; "
                f"if (-not $p.DriveLetter) {{ Add-PartitionAccessPath -DiskNumber {n} -PartitionNumber $p.PartitionNumber "
                f"-AssignDriveLetter; Start-Sleep -Seconds 1; "
                f"$p=Get-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber }}; "
                f"if (-not $p.DriveLetter) {{ throw 'Windows did not assign a drive letter.' }}; [string]$p.DriveLetter",
                timeout=300)
        letter = out.strip()[-1:]
        if not letter.isalpha():
            raise BackendError(f"Could not determine the new drive letter (PowerShell said {out.strip()!r}).", "NO_LETTER")
        return letter.upper()

    def enable_bitlocker(self, drive, letter, method, password, full_volume):
        used = "" if full_volume else "-UsedSpaceOnly"
        run_ps(f"Enable-BitLocker -MountPoint '{letter}:' -EncryptionMethod {method} {used} "
               f"-PasswordProtector -Password (ConvertTo-SecureString $env:USBLOCKBOX_PW -AsPlainText -Force) | Out-Null",
               {"USBLOCKBOX_PW": password}, timeout=180)
        run_ps(f"Add-BitLockerKeyProtector -MountPoint '{letter}:' -RecoveryPasswordProtector | Out-Null")
        out = run_ps(f"$v=Get-BitLockerVolume -MountPoint '{letter}:'; "
                     f"ConvertTo-Json -InputObject @($v.KeyProtector | ForEach-Object {{ "
                     f"[pscustomobject]@{{Id=[string]$_.KeyProtectorId;Type=[string]$_.KeyProtectorType;Rec=[string]$_.RecoveryPassword}} }})")
        prots = _as_list(_json(out))
        key = next((p["Rec"] for p in prots if p.get("Rec")), "")
        ids = [p["Id"] for p in prots]
        if not key:
            raise BackendError("BitLocker enabled but no recovery password was returned.", "NO_RECOVERY")
        return key, ids

    def wait_encrypted(self, drive, letter, progress, cancelled):
        t0 = time.time()
        last = [None, 0.0]
        while True:
            if cancelled():
                raise Cancelled("Cancelled by operator.")
            info = _json(run_ps(_BL_SCRIPT.replace("%LETTER%", letter))) or {}
            pct = float(info.get("Percent", 0))
            progress(pct / 100.0)
            state = (info.get("Status"), bool(info.get("Locked")))
            if state != last[0] or time.time() - last[1] >= 120:       # Windows' own view: status and locked or not (the percentage is logged by the app)
                diag.log.info("Encrypt disk %s (%s:): %.1f%%, status %s%s", drive.disk_number, letter, pct,
                              info.get("Status"), ", LOCKED" if info.get("Locked") else "")
                last[0], last[1] = state, time.time()
            if pct >= 100 and info.get("Status") == "FullyEncrypted":
                return
            if time.time() - t0 > 6 * 3600:
                raise BackendError("Encryption did not finish within 6 hours.", "TIMEOUT")
            time.sleep(3)

    def verify_bitlocker(self, drive, letter, method, password):
        info = _json(run_ps(_BL_SCRIPT.replace("%LETTER%", letter))) or {}
        if not info.get("Present") or info.get("Method") != method or float(info.get("Percent", 0)) < 100:
            raise BackendError(f"Verification failed: BitLocker reports {info}.", "VERIFY")
        types = _as_list(info.get("Protectors"))
        if "Password" not in types or "RecoveryPassword" not in types:
            raise BackendError(f"Verification failed: protectors are {types}.", "VERIFY")
        if not self.verify_password(DriveInfo(drive.disk_number, drive.unique_id, drive.serial,
                                                      volumes=[VolumeInfo(drive_letter=letter)]), password):
            raise BackendError("Verification failed: password did not unlock the drive.", "VERIFY")

    def finalize(self, drive, letter):
        run_ps(f"Lock-BitLocker -MountPoint '{letter}:' -ForceDismount | Out-Null")


def selftest() -> int:
    """Read-only inventory so you can check detection on real hardware before enabling real mode."""
    be = WindowsBackend()
    print("System disk numbers:", sorted(be.system_disk_numbers()))
    for d in be.list_usb_disks():
        print(json.dumps({k: v for k, v in d.__dict__.items() if k not in ("partitions", "volumes", "bitlocker")}, default=str))
        print("  partitions:", [(p.number, p.type_name, p.size_bytes, p.is_active) for p in d.partitions])
        print("  volumes:", [(v.drive_letter, v.filesystem, v.file_count) for v in d.volumes])
        print("  bitlocker:", d.bitlocker)
    return 0


def ports_dump() -> int:
    """Read-only: show the USB hubs/ports Windows reports and the connectors the app would list."""
    be = WindowsBackend()
    drives = be.list_usb_disks()
    occupied = {usbports.norm(d.location_path) for d in drives if d.location_path}
    print(usbports.dump(run_ps, occupied))
    return 0


def compare_native() -> int:
    """Read-only: ask Windows for hub and drive location paths both ways (PowerShell and directly) and print them side by
    side with the time each took. A line that says DIFFERENT is a bug in the direct route: send the output."""
    import time

    def clock(fn):
        t = time.perf_counter()
        try:
            return fn(), time.perf_counter() - t, ""
        except Exception as e:   # noqa: BLE001
            return None, time.perf_counter() - t, f"{type(e).__name__}: {e}"

    differ = 0
    print("Comparing location paths read through PowerShell and read directly from Windows (nothing is changed)...\n",
          flush=True)
    found = usbports._enumerate_hub_interfaces()
    ids = [i for i, _p in found]
    native, t_native, err = clock(lambda: usbports.native_hub_locations(ids))
    ps, t_ps, err_ps = clock(lambda: usbports.PortScanner(run_ps)._locations(ids))
    print(f"HUBS ({len(ids)}): direct {t_native:.2f} s{('  FAILED ' + err) if err else ''};  PowerShell {t_ps:.2f} s"
          f"{('  FAILED ' + err_ps) if err_ps else ''}")
    for i in ids:
        a, b = (native or {}).get(i.lower(), ""), (ps or {}).get(i.lower(), "")
        same = "same" if a == b else "DIFFERENT"
        differ += a != b
        print(f"  {same:9} {i}\n            direct:     {a or '(nothing)'}\n            PowerShell: {b or '(nothing)'}")
    print(f"  tree check on the direct answers: {'ok' if usbports.locations_plausible((native or {}).values()) else 'NOT a plausible tree'}")
    rows, t_rows, err_rows = clock(lambda: _as_list(_json(run_ps(_LIST_SCRIPT, None, 300, "compare: list USB disks"))))
    print(f"\nDRIVES ({len(rows or [])}): PowerShell listing with its own lookup {t_rows:.2f} s"
          f"{('  FAILED ' + err_rows) if err_rows else ''}")
    for r in rows or []:
        got, t_d, err_d = clock(lambda: usbports.native_disk_location(str(r.get("PnpId") or "")))
        want = (usbports.canonical_path(r.get("Location", "")), r.get("VidPid", ""))
        have = got or ("", "")
        same = "same" if have == want else "DIFFERENT"
        differ += have != want
        print(f"  {same:9} disk {r.get('Number')}  direct {t_d * 1000:.0f} ms{('  FAILED ' + err_d) if err_d else ''}"
              f"\n            direct:     {have[0] or '(nothing)'}  {have[1]}"
              f"\n            PowerShell: {want[0] or '(nothing)'}  {want[1]}")
        if r.get("T"):
            print(f"            PowerShell query times (ms): {r['T']}")
        differ += _compare_layout(r, clock)
    scanner = usbports.PortScanner(run_ps, locator=usbports.native_hub_locations)
    try:
        scanner.hubs()
    except Exception as e:   # noqa: BLE001
        print(f"\n(could not read the hubs again for the speed check: {e})")
    print("\nCONNECTION SPEED of each drive's port (read directly; USB 2.0 shows as 'high speed'):")
    for r in rows or []:
        loc = usbports.canonical_path(r.get("Location", ""))
        speed, t_s, err_s = clock(lambda: scanner.port_speed(loc))
        word = "not available" if speed is None else usbports.speed_name(speed)
        print(f"  disk {r.get('Number')}  {word}  ({t_s * 1000:.0f} ms){('  FAILED ' + err_s) if err_s else ''}")
    print("\nTo try the direct partition and volume reads in the app, set USBLOCKBOX_NATIVE_DISKS=1 before starting it.")
    print(f"\n{'All answers match.' if not differ else str(differ) + ' answer(s) are DIFFERENT. Please send this output.'}")
    return 0 if not differ else 2


def _compare_layout(r: dict, clock) -> int:
    """Print PowerShell's and the direct partition/volume answers for one disk. Returns 1 when they differ."""
    got, t, err = clock(lambda: nativedisk.native_parts_and_volumes(int(r["Number"])))
    if err or not got:
        print(f"            partitions/volumes direct: FAILED {err}")
        return 1
    style, parts, vols = got
    want_p = [(int(p["Number"]), str(p["Type"]), int(p["Size"]), bool(p["Active"]), bool(p["Hidden"]))
              for p in _as_list(r.get("Parts"))]
    have_p = [(p["Number"], p["Type"], p["Size"], p["Active"], p["Hidden"]) for p in parts]
    want_v = sorted((str(v["Letter"]), str(v["FS"]), str(v["Label"]), int(v["Size"] or 0)) for v in _as_list(r.get("Vols")))
    have_v = sorted((v["Letter"], v["FS"], v["Label"], v["Size"]) for v in vols)
    same = (style == r.get("Style") and sorted(have_p) == sorted(want_p) and have_v == want_v)
    print(f"  {'same' if same else 'DIFFERENT':9} partitions/volumes of disk {r.get('Number')}: direct {t * 1000:.0f} ms")
    if not same:
        print(f"            direct:     {style} {have_p} {have_v}\n            PowerShell: {r.get('Style')} {want_p} {want_v}")
    return 0 if same else 1


def startup_timing() -> int:
    """Read-only: time every step the first scan performs, so a slow start can be traced to its cause."""
    import time
    from .. import policy

    rows: list[tuple[float, str]] = []

    def timed(label, fn):
        t = time.perf_counter()
        note = ""
        try:
            result = fn()
        except Exception as e:   # noqa: BLE001
            result, note = None, f"   FAILED: {type(e).__name__}: {e}"
        dt = time.perf_counter() - t
        rows.append((dt, label))
        print(f"{dt:7.2f} s  {label}{note}", flush=True)
        return result

    print("Timing each step of the first scan (nothing is changed)...\n", flush=True)
    t0 = time.perf_counter()
    timed("PowerShell start-up only (no command)", lambda: run_ps("1", timeout=120))
    timed("Registry policy read", policy.read_raw)
    found = timed("Find USB hubs (SetupAPI)", usbports._enumerate_hub_interfaces) or []
    print(f"         ({len(found)} hub interface(s) found)", flush=True)
    slow_hubs = []
    t = time.perf_counter()
    for instance_id, path in found:
        h0 = time.perf_counter()
        try:
            usbports._query_hub(path)
        except Exception:   # noqa: BLE001
            pass
        slow_hubs.append((time.perf_counter() - h0, instance_id))
    total = time.perf_counter() - t
    rows.append((total, f"Ask each hub about its ports ({len(found)} hubs)"))
    print(f"{total:7.2f} s  Ask each hub about its ports ({len(found)} hubs)", flush=True)
    for dt, name in sorted(slow_hubs, reverse=True)[:3]:
        print(f"         slowest hubs: {dt:5.2f} s  {name}", flush=True)
    ids = [i for i, _p in found]
    timed(f"Look up location paths for {len(ids)} hubs (one PowerShell call)",
          lambda: usbports.PortScanner(run_ps)._locations(ids))
    be = WindowsBackend()
    timed("List USB disks (PowerShell)", be.list_usb_disks)
    timed("System disk numbers (PowerShell)", be.system_disk_numbers)
    timed("Whole port scan, cold (what the first poll does)", lambda: usbports.PortScanner(run_ps).scan(set()))
    print(f"\n{time.perf_counter() - t0:7.2f} s  total\n")
    print("Slowest steps:")
    for dt, label in sorted(rows, reverse=True)[:3]:
        print(f"  {dt:6.2f} s  {label}")
    print("\nSend this output to whoever is diagnosing the slow start.")
    return 0

