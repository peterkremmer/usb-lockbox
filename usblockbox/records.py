"""Records: one appended CSV row per run (hash-chained) and one PDF per device. No database."""
from __future__ import annotations

import csv
import hashlib
import os
import platform
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .config import Settings, current_user, method_label
from .models import RunResult, ScanResult

CSV_NAME = "usblockbox_log.csv"
REDACTED = "[not recorded - disabled in settings]"

COLUMNS = [
    "record_id", "started", "finished", "operator", "workstation", "tool_version", "backend",
    "simulation", "outcome", "error", "possible_cause",
    "serial", "unique_id", "model", "vid_pid", "firmware", "size_bytes", "hub_port",
    "scan_findings", "steps", "sanitization_category", "overwrite_passes", "destroy_note",
    "encryption_method", "filesystem", "partition_style", "volume_letter", "protector_ids",
    "password_profile", "password", "recovery_key", "settings_hash", "prev_hash", "row_hash",
]


class RecordsError(Exception):
    pass


def _safe_cell(v) -> str:
    """Neutralize spreadsheet formula injection (a leading ' is added; see README)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _row_hash(prev: str, row: dict) -> str:
    parts = [prev] + [str(row.get(c, "")) for c in COLUMNS if c not in ("row_hash", "prev_hash")]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def csv_path(settings: Settings) -> Path:
    return Path(settings.resolved_csv_dir()) / CSV_NAME


def build_row(settings: Settings, scan: ScanResult, run: RunResult, backend_name: str,
              operator: str = "") -> dict:
    d = scan.drive
    row = {
        "record_id": str(uuid.uuid4()),
        "started": run.started, "finished": run.finished,
        "operator": operator or settings.operator or current_user(),
        "workstation": platform.node(), "tool_version": __version__, "backend": backend_name,
        "simulation": "yes" if backend_name == "simulated" else "no",
        "outcome": run.outcome, "error": run.error, "possible_cause": run.possible_cause,
        "serial": d.serial, "unique_id": d.unique_id, "model": d.model, "vid_pid": d.vid_pid,
        "firmware": d.firmware, "size_bytes": d.size_bytes, "hub_port": d.location_path,
        "scan_findings": " | ".join(f"[{f.severity.value}] {f.message}" for f in scan.findings),
        "steps": " | ".join(f"{s.name}:{'ok' if s.ok else 'FAIL'}" for s in run.steps),
        "sanitization_category": run.sanitization_category,
        "overwrite_passes": settings.overwrite_passes, "destroy_note": run.destroy_note,
        "encryption_method": run.encryption_method, "filesystem": settings.filesystem,
        "partition_style": settings.partition_style, "volume_letter": run.volume_letter,
        "protector_ids": ";".join(run.protector_ids), "password_profile": run.password_profile,
        "password": run.password if settings.include_secrets_csv else (REDACTED if run.password else ""),
        "recovery_key": run.recovery_key if settings.include_secrets_csv else (REDACTED if run.recovery_key else ""),
        "settings_hash": settings.snapshot_hash(),
    }
    return row


def _last_hash(path: Path) -> str:
    if not path.exists():
        return ""
    last = ""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            last = r.get("row_hash", "")
    return last


def append_row(settings: Settings, row: dict, retries: int = 6, delay: float = 0.5) -> Path:
    """Append one row. If Excel has the file open, retry briefly, then raise RecordsError."""
    path = csv_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    for _ in range(retries):
        try:
            new = not path.exists()
            row = dict(row)
            row["prev_hash"] = _last_hash(path)
            row["row_hash"] = _row_hash(row["prev_hash"], row)
            with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                if new:
                    w.writeheader()
                w.writerow({c: _safe_cell(row.get(c, "")) if c not in ("prev_hash", "row_hash") else row.get(c, "")
                            for c in COLUMNS})
            return path
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
    raise RecordsError(f"Could not write {path} (is it open in Excel?): {last_err}")


def read_history(settings: Settings, serial: str) -> list[dict]:
    path = csv_path(settings)
    if not path.exists() or not serial:
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("serial") == serial]
    except OSError:
        return []


def verify_chain(settings: Settings) -> tuple[bool, int]:
    """(ok, first_bad_row_number). Row numbers start at 1 for the first data row."""
    path = csv_path(settings)
    if not path.exists():
        return True, 0
    prev = ""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=1):
            # undo the formula-injection prefix before re-hashing
            plain = {k: (v[1:] if v[:2] in ("'=", "'+", "'-", "'@") else v) for k, v in r.items()}
            if plain.get("prev_hash", "") != prev or _row_hash(prev, plain) != plain.get("row_hash"):
                return False, i
            prev = plain["row_hash"]
    return True, 0


# ---------------------------------------------------------------- PDF
def pdf_filename(row: dict) -> str:
    ts = re.sub(r"[^0-9]", "", row.get("finished") or row.get("started") or "")[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
    serial = re.sub(r"[^A-Za-z0-9_-]", "_", row.get("serial", "unknown"))
    return f"{ts}_{serial}.pdf"


def write_pdf(settings: Settings, scan: ScanResult, run: RunResult, row: dict) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    out_dir = Path(settings.resolved_pdf_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / pdf_filename(row)
    ss = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=8, leading=10)
    mono = ParagraphStyle("mono", parent=ss["BodyText"], fontName="Courier", fontSize=11, leading=14)
    ok = run.ok
    banner_color = colors.HexColor("#16a34a" if ok else "#dc2626")

    def tbl(rows, widths=(1.7 * inch, 5.3 * inch)):
        t = Table([[Paragraph(f"<b>{a}</b>", small), Paragraph(str(b).replace("&", "&amp;").replace("<", "&lt;"), small)]
                   for a, b in rows], colWidths=widths)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke)]))
        return t

    d = scan.drive
    story = [Paragraph("USB Lockbox Drive Record", ss["Title"])]
    b = Table([[Paragraph(f"<font color='white'><b>{run.outcome}</b></font>", ss["Heading2"])]], colWidths=[7 * inch])
    b.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), banner_color)]))
    story += [b, Spacer(1, 8)]
    story += [tbl([
        ("Record ID", row["record_id"]), ("Started / finished", f"{run.started}  /  {run.finished}"),
        ("Operator / workstation", f"{row['operator']} / {row['workstation']}"),
        ("Tool / backend", f"v{row['tool_version']} / {row['backend']}"
                           + ("  (SIMULATION - no real disk was touched)" if row["simulation"] == "yes" else "")),
    ]), Spacer(1, 8), Paragraph("<b>Drive</b>", ss["Heading3"]), tbl([
        ("Model", d.model), ("Serial", d.serial), ("Unique ID", d.unique_id), ("VID:PID", d.vid_pid),
        ("Firmware", d.firmware), ("Size", f"{d.size_bytes:,} bytes ({d.size_gb:.1f} GB)"), ("Hub port", d.location_path),
    ]), Spacer(1, 8), Paragraph("<b>Scan findings</b>", ss["Heading3"]),
        tbl([(f.severity.value, f.message) for f in scan.findings] or [("-", "none")]),
        Spacer(1, 8), Paragraph("<b>Actions</b>", ss["Heading3"]),
        tbl([(s.name, ("OK " if s.ok else "FAILED ") + s.detail) for s in run.steps] or [("-", "no actions taken")]),
        Spacer(1, 8), Paragraph("<b>Result</b>", ss["Heading3"]),
        tbl([("Sanitization achieved", run.sanitization_category or "-"),
             ("Encryption", f"{method_label(run.encryption_method) if run.encryption_method else '-'}  |  {settings.filesystem}  |  {settings.partition_style}"),
             ("Protector IDs", ";".join(run.protector_ids) or "-"),
             ("Password profile", run.password_profile),
             ("Error", run.error or "-"), ("Possible cause", run.possible_cause or "-"),
             ("Note", run.destroy_note or "-")])]

    if settings.include_secrets_pdf and (run.password or run.recovery_key):
        story += [Spacer(1, 10), Paragraph("<b>Credentials (handle as sensitive)</b>", ss["Heading3"])]
        if run.password:
            story += [Paragraph("Password:", small), Paragraph(run.password.replace("&", "&amp;").replace("<", "&lt;"), mono)]
        if run.recovery_key:
            story += [Paragraph("Recovery key:", small), Paragraph(run.recovery_key, mono)]
    else:
        story += [Spacer(1, 10), Paragraph("Credentials not included (disabled in settings).", small)]

    story += [Spacer(1, 12), Paragraph(
        f"Settings hash {row['settings_hash']} | row hash {row.get('row_hash', '(pending)')[:16]} | "
        "Overwrite on flash media is NIST SP 800-88 Clear, not Purge.", small)]
    SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                      topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                      title="USB Lockbox Drive Record").build(story)
    return path


def record_run(settings: Settings, scan: ScanResult, run: RunResult, backend_name: str,
               operator: str = "") -> tuple[Path, Path, list[str]]:
    """Write CSV row then PDF. Returns (csv_path, pdf_path, warnings)."""
    warnings: list[str] = []
    row = build_row(settings, scan, run, backend_name, operator)
    cpath = append_row(settings, row)           # raises RecordsError; caller must surface it
    try:
        row["row_hash"] = _last_hash(cpath)
        ppath = write_pdf(settings, scan, run, row)
    except Exception as e:                      # noqa: BLE001
        warnings.append(f"PDF not written: {e}")
        ppath = Path("")
    return cpath, ppath, warnings
