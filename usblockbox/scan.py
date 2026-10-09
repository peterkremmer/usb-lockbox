"""Read-only assessment of a drive. Produces findings and a verdict; never writes to the drive."""
from __future__ import annotations

from typing import Optional

from .config import Settings, method_label
from .models import (DriveInfo, Finding, PolicyReport, ScanResult, Severity, Verdict)
from .safety import eligibility_findings

BASIC_TYPES = {"basic", "microsoft basic data", "exfat", "ntfs", "fat32"}
RESERVED_MAX = 32 * 1024 * 1024   # Microsoft Reserved partition size tolerated on GPT
USB2_GB_PER_SECOND = 0.030        # what a USB 2.0 connection really moves (about 30 MB/s), not the 480 Mbit/s on the box
SLOW_LINK_TOTAL_HOURS = 1.0       # warn when the whole job would take longer than this at that speed (any drive size)


def slow_link_finding(drive: DriveInfo, passes: int) -> Optional[Finding]:
    """A note (never a block) when a drive sits on a USB 2.0 port or hub and the whole job would take over an hour."""
    if not (0 <= drive.link_speed < 3) or drive.size_gb <= 0:
        return None
    writes = max(passes, 0) + 1                    # the overwrite passes plus the encryption write
    hours = drive.size_gb / USB2_GB_PER_SECOND / 3600
    total = hours * writes
    if total < SLOW_LINK_TOTAL_HOURS:
        return None
    return Finding("SLOW_LINK", Severity.INFO,
                   f"This {drive.size_gb:.0f} GB drive is connected at USB 2.0 speed (a USB 2.0 port or hub). Each full "
                   f"write takes about {hours:.1f} h, so expect roughly {total:.1f} h in all, longer if other drives share "
                   f"the same hub. A USB 3 port or hub is much faster.")


def _history_ok(history: list[dict]) -> bool:
    """True if records show this serial was successfully provisioned by this app."""
    return any(r.get("outcome") == "PROCESSED" for r in history)


def scan_drive(drive: DriveInfo, settings: Settings, system_disks: set[int],
               policy: Optional[PolicyReport] = None,
               bound_locations: Optional[set[str]] = None,
               history: Optional[list[dict]] = None,
               extra_protected: Optional[list[str]] = None) -> ScanResult:
    res = ScanResult(drive=drive)
    add = res.findings.append
    history = history or []

    # ---- 1. eligibility (BLOCK = never touched)
    blocks = eligibility_findings(drive, settings, system_disks, bound_locations, extra_protected)
    res.findings.extend(blocks)

    # ---- 2. policy
    if policy:
        for item in policy.blocks:
            add(Finding("POLICY_" + item.name, Severity.BLOCK,
                        f"Blocked by {item.source} policy: {item.message}"))
        for item in policy.warnings:
            add(Finding("POLICYWARN_" + item.name, Severity.INFO,
                        f"Policy note ({item.source}): {item.message}"))

    if any(f.severity == Severity.BLOCK for f in res.findings):
        res.verdict = Verdict.REJECTED
        return res

    target = (policy.effective_method if policy and policy.effective_method
              else settings.encryption_method)

    # ---- 3. encryption state
    bl = drive.bitlocker
    enc_ok = False
    if not bl.present:
        add(Finding("NOT_ENCRYPTED", Severity.NEEDS_WORK, "No BitLocker encryption on this drive."))
    elif bl.locked and not bl.unlocked_with_fixed_password:
        add(Finding("ENC_LOCKED", Severity.NEEDS_WORK,
                    "Encrypted but locked and the fixed password does not open it. Contents unknown; will be wiped."))
    else:
        problems = []
        if bl.method != target:
            problems.append(f"method is {method_label(bl.method)}, target is {method_label(target)}")
        if bl.percent_encrypted < 100:
            problems.append(f"only {bl.percent_encrypted:.0f}% encrypted")
        if not bl.protection_on:
            problems.append("protection is off")
        if not bl.unlocked_with_fixed_password:
            problems.append("fixed password was not verified")
        if problems:
            add(Finding("ENC_WRONG", Severity.NEEDS_WORK, "Encryption not to standard: " + "; ".join(problems) + "."))
        else:
            enc_ok = True
            add(Finding("ENC_OK", Severity.INFO, f"Encrypted with {method_label(target)}, fully encrypted, the fixed password opens it."))

    slow = slow_link_finding(drive, settings.overwrite_passes)
    if slow:
        add(slow)

    # ---- 4. content
    files = drive.total_files
    if files is None:
        if bl.present:
            pass   # already covered by locked finding
        else:
            add(Finding("CONTENT_UNKNOWN", Severity.NEEDS_WORK, "Could not read the drive's contents."))
    elif files > 0:
        res.content_found = True
        add(Finding("CONTENT", Severity.NEEDS_WORK,
                    f"{files} file(s) on the drive ({drive.file_detail}); will be permanently erased."
                    if drive.file_detail else f"{files} file(s) on the drive; will be permanently erased."))
    else:
        add(Finding("EMPTY", Severity.INFO, "No files found."))

    # ---- 5. partition / boot oddities
    layout_ok = True
    if drive.partition_style in ("", "RAW") and not drive.volumes:
        add(Finding("RAW", Severity.NEEDS_WORK, "Drive has no partition table."))
        layout_ok = False
    elif drive.partition_style != settings.partition_style:
        add(Finding("STYLE", Severity.NEEDS_WORK,
                    f"Partition style is {drive.partition_style}, target is {settings.partition_style}."))
        layout_ok = False
    real = [p for p in drive.partitions if not (p.size_bytes <= RESERVED_MAX and p.type_name.lower() in ("reserved", "microsoft reserved"))]
    if len(real) > 1:
        add(Finding("MULTI_PART", Severity.NEEDS_WORK, f"{len(real)} partitions found; target is one."))
        layout_ok = False
    for p in real:
        if p.type_name.lower() not in BASIC_TYPES:
            add(Finding("ODD_PART", Severity.NEEDS_WORK,
                        f"Unusual partition type '{p.type_name}' (partition {p.number})."))
            layout_ok = False
        if p.is_active:
            add(Finding("ACTIVE_FLAG", Severity.NEEDS_WORK, f"Partition {p.number} is flagged bootable/active."))
            layout_ok = False
        if p.is_hidden:
            add(Finding("HIDDEN_PART", Severity.NEEDS_WORK, f"Partition {p.number} is hidden."))
            layout_ok = False
    if drive.mbr_boot_code_present:
        add(Finding("BOOT_CODE", Severity.NEEDS_WORK, "Boot code found in the first sector (bootloader or installer media)."))
        layout_ok = False
    for v in drive.volumes:
        if v.filesystem and v.filesystem.lower() not in (settings.filesystem.lower(), "") and not bl.present:
            add(Finding("FS", Severity.NEEDS_WORK, f"Filesystem is {v.filesystem}, target is {settings.filesystem}."))
            layout_ok = False

    # ---- 6. history / sanitization honesty
    res.history_known = _history_ok(history)
    if res.history_known:
        add(Finding("HISTORY", Severity.INFO, f"Serial found in records ({len(history)} prior run(s))."))
    else:
        add(Finding("HISTORY_NONE", Severity.INFO,
                    "Drive not in records: prior use unknown. Overwrite counts as Clear only on flash media."))

    # ---- verdict
    needs = [f for f in res.findings if f.severity == Severity.NEEDS_WORK]
    compliant_now = enc_ok and layout_ok and files == 0 and not needs
    if compliant_now and settings.reuse_compliant_drives and res.history_known:
        res.verdict = Verdict.ALREADY_OK
    elif compliant_now:
        add(Finding("REFRESH", Severity.NEEDS_WORK,
                    "Looks compliant and empty, but prior use is not provable and deleted data may remain. "
                    "Re-provisioning is recommended (change in Settings > Safety if you accept that risk)."))
        res.verdict = Verdict.NEEDS_WORK
    else:
        res.verdict = Verdict.NEEDS_WORK
    return res
