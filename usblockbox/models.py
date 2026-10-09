"""Plain data structures shared by every layer. No I/O in this module."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class SlotState(str, Enum):
    EMPTY = "EMPTY"
    SCANNING = "SCANNING"
    NEEDS_WORK = "NEEDS_WORK"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    ALREADY_OK = "ALREADY_OK"
    REJECTED = "REJECTED"      # rejected by scan/safety/policy (never touched)
    FAILED = "FAILED"          # processing started and did not complete


class Severity(str, Enum):
    INFO = "INFO"
    NEEDS_WORK = "NEEDS_WORK"
    BLOCK = "BLOCK"


class Verdict(str, Enum):
    ALREADY_OK = "ALREADY_OK"
    NEEDS_WORK = "NEEDS_WORK"
    REJECTED = "REJECTED"


@dataclass
class PartitionInfo:
    number: int
    type_name: str            # e.g. "Basic", "EFI System", "Recovery", "Linux", "Unknown"
    size_bytes: int
    is_active: bool = False   # MBR active/boot flag
    is_hidden: bool = False


@dataclass
class VolumeInfo:
    drive_letter: str = ""    # "E" (no colon) or ""
    filesystem: str = ""      # exFAT, NTFS, FAT32, RAW, ""
    label: str = ""
    size_bytes: int = 0
    used_bytes: int = 0
    file_count: Optional[int] = None   # None = could not enumerate (locked / unreadable)
    file_detail: str = ""              # where the files are, e.g. "2 at the top level; 6386 in .cache (hidden)"


@dataclass
class BitLockerInfo:
    present: bool = False
    protection_on: bool = False
    method: str = ""          # "XtsAes256", "XtsAes128", "Aes256", "Aes128", ...
    percent_encrypted: float = 0.0
    locked: bool = False
    protector_types: list[str] = field(default_factory=list)
    unlocked_with_fixed_password: bool = False
    fully_encrypted: bool = True           # Windows says FullyEncrypted (not converting, not suspended)


def clean_serial(raw) -> str:
    """A serial number fit to show and to record: printable ASCII only. Some drives report padding, control characters
    or text in another encoding, which showed up as a box or a stray symbol."""
    s = "".join(ch for ch in str(raw or "") if " " <= ch <= "~")
    return s.strip()


@dataclass
class DriveInfo:
    disk_number: int
    unique_id: str
    serial: str
    vid_pid: str = ""
    model: str = ""
    firmware: str = ""
    size_bytes: int = 0
    bus_type: str = "USB"
    is_removable: bool = True
    is_system: bool = False
    is_boot: bool = False
    has_pagefile: bool = False
    is_read_only: bool = False
    location_path: str = ""        # USB hub/port location path used for slot binding
    partition_style: str = ""      # "GPT", "MBR", "RAW"
    partitions: list[PartitionInfo] = field(default_factory=list)
    volumes: list[VolumeInfo] = field(default_factory=list)
    bitlocker: BitLockerInfo = field(default_factory=BitLockerInfo)
    mbr_boot_code_present: bool = False
    hardware_encrypted_suspected: bool = False
    lun_count: int = 1
    capacity_mismatch: bool = False   # set by optional capacity test
    link_chain: list = field(default_factory=list)   # [{"kind": "drive"|"hub", "path", "speed"}] from its port outward
    link_speed: int = -1           # USB speed of the connection: 0 low, 1 full, 2 high (USB 2.0), 3+ SuperSpeed; -1 = unknown

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1_000_000_000

    @property
    def total_files(self) -> Optional[int]:
        counts = [v.file_count for v in self.volumes]
        if not counts:
            return 0
        if any(c is None for c in counts):
            return None
        return sum(c for c in counts if c is not None)

    @property
    def file_detail(self) -> str:
        return "; ".join(v.file_detail for v in self.volumes if v.file_detail)

    def identity(self) -> dict:
        return {"disk_number": self.disk_number, "unique_id": self.unique_id,
                "serial": self.serial, "size_bytes": self.size_bytes}


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str


@dataclass
class ScanResult:
    drive: DriveInfo
    findings: list[Finding] = field(default_factory=list)
    verdict: Verdict = Verdict.NEEDS_WORK
    history_known: bool = False          # serial seen before as encrypted-from-first-use
    content_found: bool = False

    def reasons(self, severity: Optional[Severity] = None) -> list[str]:
        return [f.message for f in self.findings
                if severity is None or f.severity == severity]


@dataclass
class PolicyItem:
    name: str
    value: str
    source: str               # "GPO", "Intune/MDM", "derived"
    status: str               # "ok", "warn", "block"
    message: str = ""


@dataclass
class PolicyReport:
    items: list[PolicyItem] = field(default_factory=list)
    available: bool = True    # False when the probe could not run (non-Windows / simulated)
    note: str = ""
    effective_method: str = ""   # encryption method that will really be used ("" = requested one)
    min_password_length: int = 0
    password_complexity_required: bool = False

    @property
    def blocks(self) -> list[PolicyItem]:
        return [i for i in self.items if i.status == "block"]

    @property
    def warnings(self) -> list[PolicyItem]:
        return [i for i in self.items if i.status == "warn"]


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    started: str = ""
    finished: str = ""


@dataclass
class RunResult:
    ok: bool
    outcome: str                         # "PROCESSED", "ALREADY_COMPLIANT", "FAILED", "REJECTED"
    steps: list[StepResult] = field(default_factory=list)
    error: str = ""
    possible_cause: str = ""
    password: str = ""
    password_profile: str = "default"
    recovery_key: str = ""
    protector_ids: list[str] = field(default_factory=list)
    encryption_method: str = ""
    sanitization_category: str = ""
    destroy_note: str = ""
    volume_letter: str = ""
    started: str = ""
    finished: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Port:
    """One physical USB connector as the user sees it (a USB 3 connector is two bus ports; they are merged)."""
    key: str                          # stable id: lower-case location path of the lowest member
    paths: tuple = ()                 # every lower-case location path that belongs to this connector
    where: str = ""                   # "this computer", "hub 1", "virtual"
    name: str = ""                    # the operator's own label for this port (Settings > Ports)
