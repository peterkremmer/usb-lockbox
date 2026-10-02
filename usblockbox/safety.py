"""Safety guards. Every destructive step re-runs these immediately beforehand."""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .models import DriveInfo, Finding, Severity


def _norm(p: str) -> str:
    try:
        return str(Path(p).resolve()).lower().rstrip("\\/")
    except OSError:
        return p.lower().rstrip("\\/")


def protected_paths(settings: Settings, extra: list[str] | None = None) -> list[str]:
    """Paths that must never live on a drive we are about to wipe."""
    paths = [settings.resolved_csv_dir(), settings.resolved_pdf_dir(), str(Path(__file__).resolve().parent)]
    paths += extra or []
    return [p for p in paths if p]


def _drive_letter_of(path: str) -> str:
    p = path.strip()
    return p[0].upper() if len(p) >= 2 and p[1] == ":" else ""


def eligibility_findings(drive: DriveInfo, settings: Settings,
                         system_disks: set[int], bound_locations: set[str] | None = None,
                         extra_protected: list[str] | None = None) -> list[Finding]:
    """BLOCK findings mean the drive must never be written to."""
    out: list[Finding] = []

    def block(code: str, msg: str) -> None:
        out.append(Finding(code, Severity.BLOCK, msg))

    if drive.disk_number in system_disks or drive.is_system or drive.is_boot or drive.has_pagefile:
        block("SYSTEM_DISK", "This is a system/boot disk. Never touched.")
    if drive.bus_type.upper() != "USB":
        block("NOT_USB", f"Bus type is {drive.bus_type}, not USB.")
    if not drive.is_removable and not settings.allow_fixed_disks:
        block("FIXED_DISK", "Drive reports as fixed (not removable) and fixed disks are disabled in settings.")
    if drive.is_read_only:
        block("WRITE_PROTECTED", "Drive is write-protected (hardware switch or read-only flag).")
    if drive.hardware_encrypted_suspected:
        block("HW_ENCRYPTED", "Looks like a hardware-encrypted or virtual-CD device. Not supported; set aside.")
    if drive.lun_count > 1:
        block("MULTI_LUN", f"Device exposes {drive.lun_count} logical units. Not supported; set aside.")
    if drive.capacity_mismatch:
        block("CAPACITY_MISMATCH", "Capacity test failed: drive reports more space than it has. Counterfeit or faulty.")
    if drive.size_gb < settings.min_size_gb:
        block("TOO_SMALL", f"Size {drive.size_gb:.1f} GB is below the minimum ({settings.min_size_gb} GB).")
    if drive.size_gb > settings.max_size_gb:
        block("TOO_LARGE", f"Size {drive.size_gb:.1f} GB exceeds the maximum ({settings.max_size_gb} GB).")
    if not drive.serial:
        block("NO_SERIAL", "Drive reports no serial number, so it cannot be tracked in records.")
    if bound_locations is not None and drive.location_path.lower() not in {b.lower() for b in bound_locations}:
        block("UNBOUND_PORT", "Drive is not in one of the USB ports this app lists. Move it to a listed port.")

    # Never wipe the drive that holds the records folders or the app itself.
    letters = {v.drive_letter.upper() for v in drive.volumes if v.drive_letter}
    for p in protected_paths(settings, extra_protected):
        if _drive_letter_of(p) in letters and _drive_letter_of(p):
            block("HOLDS_APP_DATA", f"This drive holds the app or its records folder ({p}).")
            break
    return out


class SafetyError(Exception):
    pass


def revalidate(backend, expected: DriveInfo, settings: Settings,
               bound_locations: set[str] | None = None,
               extra_protected: list[str] | None = None) -> DriveInfo:
    """Fetch the drive afresh and prove it is still the same, still eligible device.

    Disk numbers can change when hubs re-enumerate, so identity is serial + unique id + size,
    not just the disk number.
    """
    fresh = backend.get_drive(expected.disk_number)
    if fresh is None:
        raise SafetyError("Drive is no longer present (removed or re-enumerated).")
    if (fresh.unique_id != expected.unique_id or fresh.serial != expected.serial
            or fresh.size_bytes != expected.size_bytes):
        raise SafetyError("Disk number now points at a different device. Aborting.")
    system_disks = backend.system_disk_numbers()
    blocks = [f for f in eligibility_findings(fresh, settings, system_disks, bound_locations,
                                              extra_protected)
              if f.severity == Severity.BLOCK]
    if blocks:
        raise SafetyError("Safety re-check failed: " + "; ".join(b.message for b in blocks))
    return fresh
