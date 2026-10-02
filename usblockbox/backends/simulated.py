"""Simulated backend: virtual drives in memory. Nothing here can touch a real disk."""
from __future__ import annotations

import copy
import random
import threading
import time
from typing import Optional

from ..models import BitLockerInfo, DriveInfo, PartitionInfo, Port, VolumeInfo
from .base import Backend, BackendError, Cancelled, CancelCheck, Progress

GB = 1_000_000_000
STATION_PW_DEFAULT = "Sim-Station-Pass-2026"   # dummy value for the simulator and tests only

SCENARIOS = [
    "blank", "used_files", "compliant_empty", "wrong_method", "odd_boot",
    "hw_encrypted", "system_disk", "write_protected", "too_small", "locked_unknown",
]


def _drive(n: int, serial: str, **kw) -> DriveInfo:
    d = DriveInfo(disk_number=n, unique_id=f"SIMUID-{serial}", serial=serial,
                  vid_pid="0781:5581", model="SimFlash Cruzer", firmware="1.00",
                  size_bytes=32 * GB, location_path=f"sim-port-{n}",
                  partition_style="RAW")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


class SimulatedBackend(Backend):
    name = "simulated"
    real = False

    def __init__(self, speed: float = 1.0, station_password: str = STATION_PW_DEFAULT, port_count: int = 4):
        self.port_count = port_count
        self.speed = speed                  # >1 is faster. Tests use a large value.
        self.station_password = station_password
        self._lock = threading.RLock()
        self._disks: dict[int, DriveInfo] = {}
        self._passwords: dict[str, str] = {}      # serial -> password protecting the drive
        self._fail_at: dict[str, str] = {}        # serial -> step name to fail at
        self._removed_midway: set[str] = set()
        self.system_disks: set[int] = {0}
        self.log: list[str] = []
        self._letters = iter("EFGHIJKLMNOPQRSTUVWXYZ")

    # ------------------------------------------------------------ test/dev controls
    def add_scenario(self, scenario: str, port: int) -> DriveInfo:
        serial = f"SIM{scenario[:4].upper()}{random.randint(1000, 9999)}"
        with self._lock:
            n = 100 + port
            d = _drive(n, serial)
            d.location_path = f"sim-port-{port}"
            vol = lambda files=0, fs="exFAT", letter=None: VolumeInfo(  # noqa: E731
                drive_letter=letter or next(self._letters), filesystem=fs, label="USB",
                size_bytes=d.size_bytes, used_bytes=files * 4_000_000, file_count=files)
            basic = PartitionInfo(1, "Basic", d.size_bytes - 1_000_000)
            if scenario == "blank":
                pass
            elif scenario == "used_files":
                d.partition_style = "MBR"; d.partitions = [basic]; d.volumes = [vol(37, "NTFS")]
            elif scenario == "compliant_empty":
                d.partition_style = "GPT"; d.partitions = [basic]; d.volumes = [vol(0)]
                d.bitlocker = BitLockerInfo(True, True, "XtsAes256", 100.0, False, ["Password", "RecoveryPassword"], True)
                self._passwords[serial] = self.station_password
            elif scenario == "wrong_method":
                d.partition_style = "GPT"; d.partitions = [basic]; d.volumes = [vol(5)]
                d.bitlocker = BitLockerInfo(True, True, "XtsAes128", 100.0, False, ["Password"], True)
                self._passwords[serial] = self.station_password
            elif scenario == "odd_boot":
                d.partition_style = "MBR"; d.mbr_boot_code_present = True
                d.partitions = [PartitionInfo(1, "Linux", 8 * GB, True), PartitionInfo(2, "EFI System", 100_000_000),
                                PartitionInfo(3, "Basic", 20 * GB)]
                d.volumes = [vol(0, "ext4")]
            elif scenario == "hw_encrypted":
                d.model = "Apricorn Aegis Secure Key"; d.hardware_encrypted_suspected = True
            elif scenario == "system_disk":
                d.disk_number = 0; d.is_system = True; d.is_boot = True; d.bus_type = "USB"
                d.partition_style = "GPT"; d.volumes = [vol(120000, "NTFS", "C")]
                d.partitions = [PartitionInfo(1, "Basic", d.size_bytes)]
            elif scenario == "write_protected":
                d.is_read_only = True
                d.partition_style = "MBR"; d.partitions = [basic]; d.volumes = [vol(3, "FAT32")]
            elif scenario == "too_small":
                d.size_bytes = GB // 2
            elif scenario == "locked_unknown":
                d.partition_style = "GPT"; d.partitions = [basic]; d.volumes = [VolumeInfo(file_count=None, filesystem="")]
                d.bitlocker = BitLockerInfo(True, True, "XtsAes256", 100.0, True, ["Password"], False)
                self._passwords[serial] = "someone-elses-password"
            else:
                raise ValueError(scenario)
            self._disks[d.disk_number] = d
            return copy.deepcopy(d)

    def remove(self, serial: str) -> None:
        """Pull a drive out (also works mid-operation to test surprise removal)."""
        with self._lock:
            for n, d in list(self._disks.items()):
                if d.serial == serial:
                    del self._disks[n]
                    self._removed_midway.add(serial)

    def list_ports(self, occupied):
        return [Port(f"sim-port-{i}", (f"sim-port-{i}",), "virtual") for i in range(1, self.port_count + 1)]

    def set_port_count(self, n: int) -> None:
        """Add or remove virtual ports (a removed port loses its drive)."""
        n = max(1, min(16, int(n)))
        for p in range(n + 1, self.port_count + 1):
            self.remove_port(p)
        self.port_count = n

    def remove_port(self, port: int) -> None:
        with self._lock:
            for n, d in list(self._disks.items()):
                if d.location_path == f"sim-port-{port}":
                    self.remove(d.serial)

    def fail_at(self, serial: str, step: str) -> None:
        self._fail_at[serial] = step

    # ------------------------------------------------------------ read-only
    def list_usb_disks(self) -> list[DriveInfo]:
        with self._lock:
            return [copy.deepcopy(d) for d in self._disks.values()]

    def get_drive(self, disk_number: int) -> Optional[DriveInfo]:
        with self._lock:
            d = self._disks.get(disk_number)
            return copy.deepcopy(d) if d else None

    def system_disk_numbers(self) -> set[int]:
        return set(self.system_disks)

    def verify_station_password(self, drive: DriveInfo, password: str) -> bool:
        with self._lock:
            d = self._disks.get(drive.disk_number)
            return bool(d and self._passwords.get(d.serial) == password)

    # ------------------------------------------------------------ helpers
    def _get(self, drive: DriveInfo, step: str) -> DriveInfo:
        with self._lock:
            d = self._disks.get(drive.disk_number)
            if d is None or d.serial != drive.serial:
                raise BackendError("Device was removed during the operation.", "REMOVED")
            if self._fail_at.get(d.serial) == step:
                raise BackendError(f"Injected failure at {step}: Access is denied. (0x80070005)", "ACCESS")
            return d

    def _sleep(self, seconds: float, cancelled: CancelCheck | None = None) -> None:
        end = time.time() + seconds / max(self.speed, 0.001)
        while time.time() < end:
            if cancelled and cancelled():
                raise Cancelled("Cancelled by operator.")
            time.sleep(min(0.02, max(end - time.time(), 0)))

    # ------------------------------------------------------------ destructive
    def clear_disk(self, drive):
        d = self._get(drive, "clear_disk")
        self._sleep(0.3)
        with self._lock:
            d.partitions, d.volumes = [], []
            d.partition_style = "RAW"; d.mbr_boot_code_present = False
            d.bitlocker = BitLockerInfo()
            self._passwords.pop(d.serial, None)
        self.log.append(f"clear_disk {d.serial}")

    def zero_edges(self, drive, mb):
        self._get(drive, "zero_edges"); self._sleep(0.2)
        self.log.append(f"zero_edges {drive.serial} {mb}MB")

    def overwrite(self, drive, passes, verify, progress, cancelled):
        for p in range(passes):
            for i in range(20):
                self._get(drive, "overwrite")
                self._sleep(0.05, cancelled)
                progress((p + i / 20) / max(passes, 1))
        if verify:
            self._get(drive, "overwrite_verify"); self._sleep(0.1)
        progress(1.0)
        self.log.append(f"overwrite {drive.serial} x{passes}")

    def init_partition_format(self, drive, style, fs, label):
        d = self._get(drive, "format")
        self._sleep(0.3)
        letter = next(self._letters)
        with self._lock:
            d.partition_style = style
            d.partitions = [PartitionInfo(1, "Basic", d.size_bytes - 16_000_000)]
            d.volumes = [VolumeInfo(letter, fs, label, d.size_bytes, 0, 0)]
        self.log.append(f"format {drive.serial} {style}/{fs}")
        return letter

    def enable_bitlocker(self, drive, letter, method, password, full_volume):
        d = self._get(drive, "enable_bitlocker")
        self._sleep(0.2)
        key = "-".join(f"{random.randint(0, 999999):06d}" for _ in range(8))
        with self._lock:
            d.bitlocker = BitLockerInfo(True, True, method, 0.0, False,
                                        ["Password", "RecoveryPassword"], True)
            self._passwords[d.serial] = password
        self.log.append(f"enable_bitlocker {drive.serial} {method} full={full_volume}")
        return key, [f"{{SIM-{random.randint(10**7, 10**8)}}}", f"{{SIM-{random.randint(10**7, 10**8)}}}"]

    def wait_encrypted(self, drive, letter, progress, cancelled):
        d = self._get(drive, "wait_encrypted")
        for i in range(1, 21):
            self._get(drive, "wait_encrypted")
            self._sleep(0.06, cancelled)
            with self._lock:
                d.bitlocker.percent_encrypted = i * 5.0
            progress(i / 20)

    def verify_bitlocker(self, drive, letter, method, password):
        d = self._get(drive, "verify")
        bl = d.bitlocker
        if not (bl.present and bl.method == method and bl.percent_encrypted >= 100):
            raise BackendError("Verification failed: encryption state is not as expected.", "VERIFY")
        if self._passwords.get(d.serial) != password:
            raise BackendError("Verification failed: password did not unlock the drive.", "VERIFY")

    def finalize(self, drive, letter):
        d = self._get(drive, "finalize")
        with self._lock:
            d.bitlocker.locked = True
