"""Backend interface: everything that touches a disk lives behind this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..models import DriveInfo, Port

Progress = Callable[[float], None]       # fraction 0..1
CancelCheck = Callable[[], bool]         # True = stop now


class BackendError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class Cancelled(BackendError):
    pass


class Backend(ABC):
    name = "abstract"
    real = False    # True only for backends that really write to disks
    elevated = True      # real backends set this False when they lack administrator rights
    ports_note = ""      # shown to the user when the port list is incomplete or unavailable

    def list_ports(self, occupied: set[str]) -> list[Port]:
        """The USB connectors this computer has (and any hub plugged in). `occupied` holds the lower-case
        location paths of the USB drives currently present. Read-only."""
        return []

    def hold(self, disk_number: int) -> None:
        """Processing of this disk is starting: do not touch it from the read-only listing until release()."""

    def release(self, disk_number: int) -> None:
        """Processing of this disk has ended."""

    def link_chain(self, drive: DriveInfo) -> list:
        """The USB links between this drive and the computer, from its own port outward:
        [{"kind": "drive"|"hub", "path": location path, "speed": USB speed or None}]. Empty when unknown."""
        return []

    def prove_fixed_password(self, drive: DriveInfo, password: str) -> None:
        """Set drive.bitlocker.unlocked_with_fixed_password for an already-encrypted drive. Listing never does this,
        because the test locks the volume. Backends whose drives already carry the answer do nothing."""

    def unreadable_drives(self) -> list[tuple[int, str]]:
        """[(location path, why)] of drives that are plugged in but could not be read on the latest listing, so their tile
        can say so instead of looking empty. Backends that cannot fail to read a drive return nothing."""
        return []

    # ---- read-only
    @abstractmethod
    def list_usb_disks(self) -> list[DriveInfo]: ...

    @abstractmethod
    def get_drive(self, disk_number: int) -> Optional[DriveInfo]: ...

    @abstractmethod
    def system_disk_numbers(self) -> set[int]: ...

    @abstractmethod
    def verify_password(self, drive: DriveInfo, password: str) -> bool: ...

    # ---- destructive (callers must have run safety.revalidate first)
    @abstractmethod
    def clear_disk(self, drive: DriveInfo) -> None: ...

    @abstractmethod
    def zero_edges(self, drive: DriveInfo, mb: int) -> None: ...

    @abstractmethod
    def overwrite(self, drive: DriveInfo, passes: int, verify: bool,
                  progress: Progress, cancelled: CancelCheck) -> None: ...

    @abstractmethod
    def init_partition_format(self, drive: DriveInfo, style: str, fs: str, label: str) -> str:
        """Create one partition and format it. Returns drive letter (no colon)."""

    @abstractmethod
    def enable_bitlocker(self, drive: DriveInfo, letter: str, method: str, password: str,
                         full_volume: bool) -> tuple[str, list[str]]:
        """Returns (recovery_password, protector_ids)."""

    @abstractmethod
    def wait_encrypted(self, drive: DriveInfo, letter: str, progress: Progress,
                       cancelled: CancelCheck) -> None: ...

    @abstractmethod
    def verify_bitlocker(self, drive: DriveInfo, letter: str, method: str, password: str) -> None:
        """Raise BackendError if method/percent/protectors/unlock test do not check out."""

    @abstractmethod
    def finalize(self, drive: DriveInfo, letter: str) -> None:
        """Lock/dismount so the drive is safe to remove."""
