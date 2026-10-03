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

from .. import usbports
from ..junk import junk_label
from ..models import BitLockerInfo, DriveInfo, PartitionInfo, Port, VolumeInfo
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


def run_ps(script: str, env_extra: Optional[dict] = None, timeout: int = 120) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackendError(f"PowerShell failed to run: {e}", "PS_LAUNCH")
    if p.returncode != 0:
        raise BackendError((p.stderr or p.stdout).strip() or f"PowerShell exit {p.returncode}", "PS_ERROR")
    return p.stdout


def _json(text: str):
    text = text.strip()
    return json.loads(text) if text else None


def _as_list(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


_LIST_SCRIPT = r"""
$ErrorActionPreference='Stop'
$out=@()
foreach($d in (Get-Disk | Where-Object { $_.BusType -eq 'USB' })){
  $n=$d.Number
  $wmi=Get-CimInstance Win32_DiskDrive | Where-Object { $_.Index -eq $n }
  $parts=@(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{Number=$_.PartitionNumber;Type=[string]$_.Type;Size=[int64]$_.Size;Active=[bool]$_.IsActive;Hidden=[bool]$_.IsHidden;Letter=[string]$_.DriveLetter}})
  $vols=@(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue | Where-Object {$_.DriveLetter} | ForEach-Object {
    $v=Get-Volume -DriveLetter $_.DriveLetter -ErrorAction SilentlyContinue
    [pscustomobject]@{Letter=[string]$_.DriveLetter;FS=[string]$v.FileSystem;Label=[string]$v.FileSystemLabel;Size=[int64]$v.Size;Free=[int64]$v.SizeRemaining}})
  $loc='';$vidpid=''
  try{
    $pnp=$wmi.PNPDeviceID
    $parent=(Get-PnpDeviceProperty -InstanceId $pnp -KeyName 'DEVPKEY_Device_Parent').Data
    $loc=((Get-PnpDeviceProperty -InstanceId $parent -KeyName 'DEVPKEY_Device_LocationPaths').Data) -join ';'
    if($parent -match 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})'){ $vidpid=$matches[1]+':'+$matches[2] }
  }catch{}
  $out+=[pscustomobject]@{
    Number=$n;UniqueId=[string]$d.UniqueId;Serial=([string]$d.SerialNumber).Trim();Model=[string]$d.FriendlyName
    Firmware=[string]$d.FirmwareVersion;Size=[int64]$d.Size;Bus=[string]$d.BusType;IsSystem=[bool]$d.IsSystem
    IsBoot=[bool]$d.IsBoot;ReadOnly=[bool]$d.IsReadOnly;Style=[string]$d.PartitionStyle
    Media=[string]$wmi.MediaType;Location=$loc;VidPid=$vidpid;Parts=$parts;Vols=$vols}
}
ConvertTo-Json -InputObject @($out) -Depth 6
"""

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
        self._scanner = usbports.PortScanner(run_ps)
        self.ports_note = ""

    def list_ports(self, occupied):
        ports, self.ports_note = self._scanner.scan(occupied)
        return ports

    # ------------------------------------------------------------ read-only
    def system_disk_numbers(self) -> set[int]:
        out = run_ps(r"""
$n=@(Get-Disk | Where-Object { $_.IsSystem -or $_.IsBoot } | ForEach-Object { $_.Number })
$sd=$env:SystemDrive.Substring(0,1)
$n+=@(Get-Partition -DriveLetter $sd | ForEach-Object { $_.DiskNumber })
Get-CimInstance Win32_PageFileUsage | ForEach-Object { $l=$_.Name.Substring(0,1); $n+=@(Get-Partition -DriveLetter $l -ErrorAction SilentlyContinue | ForEach-Object { $_.DiskNumber }) }
ConvertTo-Json -InputObject @($n | Select-Object -Unique)
""")
        return {int(x) for x in _as_list(_json(out))}

    def list_usb_disks(self) -> list[DriveInfo]:
        rows = _as_list(_json(run_ps(_LIST_SCRIPT, timeout=90)))
        return [self._to_drive(r) for r in rows]

    def get_drive(self, disk_number: int) -> Optional[DriveInfo]:
        # Enumerating all USB disks keeps one code path; the list is small.
        for d in self.list_usb_disks():
            if d.disk_number == disk_number:
                return d
        return None

    def _to_drive(self, r: dict) -> DriveInfo:
        model = r.get("Model") or ""
        d = DriveInfo(
            disk_number=int(r["Number"]), unique_id=r.get("UniqueId", ""), serial=r.get("Serial", ""),
            vid_pid=r.get("VidPid", ""), model=model, firmware=r.get("Firmware", ""),
            size_bytes=int(r.get("Size", 0)), bus_type=r.get("Bus", ""),
            is_removable=("removable" in (r.get("Media") or "").lower()),   # VERIFY on your drive models
            is_system=bool(r.get("IsSystem")), is_boot=bool(r.get("IsBoot")),
            is_read_only=bool(r.get("ReadOnly")), location_path=usbports.canonical_path(r.get("Location", "")),
            partition_style=r.get("Style", ""))
        d.hardware_encrypted_suspected = any(h in model.lower() for h in HW_ENCRYPTED_HINTS)  # VERIFY
        for p in _as_list(r.get("Parts")):
            d.partitions.append(PartitionInfo(int(p["Number"]), str(p["Type"]), int(p["Size"]),
                                              bool(p["Active"]), bool(p["Hidden"])))
        for v in _as_list(r.get("Vols")):
            d.volumes.append(VolumeInfo(v["Letter"], v["FS"], v["Label"], int(v["Size"] or 0),
                                        int((v["Size"] or 0) - (v["Free"] or 0)), None))
        d.mbr_boot_code_present = self._boot_code_present(d.disk_number)
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
                if not d.bitlocker.locked:
                    pw = self._pw()
                    d.bitlocker.unlocked_with_fixed_password = bool(pw) and self.verify_password(d, pw)
                else:
                    pw = self._pw()
                    if pw and self.verify_password(d, pw):
                        d.bitlocker.unlocked_with_fixed_password = True
                        d.bitlocker.locked = False
            if not (d.bitlocker.present and d.bitlocker.locked):
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
            self._diskpart(f"select disk {n}\nclean\nconvert {style.lower()}\n")
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
        while True:
            if cancelled():
                raise Cancelled("Cancelled by operator.")
            info = _json(run_ps(_BL_SCRIPT.replace("%LETTER%", letter))) or {}
            pct = float(info.get("Percent", 0))
            progress(pct / 100.0)
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
