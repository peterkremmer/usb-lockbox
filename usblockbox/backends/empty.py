"""Backend for computers where drives cannot be read (anything that is not Windows). It never sees a drive."""
from __future__ import annotations

from ..models import DriveInfo
from .base import Backend, BackendError


class NoHardwareBackend(Backend):
    name = "none"
    real = False
    ports_note = "Reading USB ports and drives needs Windows. Use Simulator mode to try the app on virtual drives."

    def list_usb_disks(self) -> list[DriveInfo]:
        return []

    def get_drive(self, disk_number):
        return None

    def system_disk_numbers(self) -> set[int]:
        return set()

    def verify_password(self, drive, password) -> bool:
        return False

    def _no(self, *a, **k):
        raise BackendError("No drive access on this computer.", "NO_HARDWARE")

    clear_disk = zero_edges = overwrite = init_partition_format = enable_bitlocker = _no
    wait_encrypted = verify_bitlocker = finalize = _no
