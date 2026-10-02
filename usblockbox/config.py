"""Settings: JSON on disk (in <app folder>/data by default), secrets protected with Windows DPAPI (user scope)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

from .appinfo import APP_SLUG

IS_WINDOWS = platform.system() == "Windows"

ENCRYPTION_METHODS = ["XtsAes256", "XtsAes128", "Aes256", "Aes128"]      # the names PowerShell's Enable-BitLocker takes
# How people (and Microsoft's own docs) write them. "Aes256"/"Aes128" are the older CBC mode.
METHOD_LABELS = {"XtsAes256": "XTS-AES-256", "XtsAes128": "XTS-AES-128", "Aes256": "AES-CBC-256", "Aes128": "AES-CBC-128"}


def current_user() -> str:
    """The signed-in user, used as the operator name in records."""
    import getpass
    try:
        return getpass.getuser()
    except Exception:   # noqa: BLE001
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def method_label(name: str) -> str:
    return METHOD_LABELS.get(name, name or "unknown")


def fips_mode_enabled():
    """True/False if Windows' "Use FIPS compliant algorithms" policy is on/off; None if it cannot be read.
    VERIFY the registry location on a real machine."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Lsa\FipsAlgorithmPolicy") as k:
            return bool(winreg.QueryValueEx(k, "Enabled")[0])
    except OSError:
        return None
PASSWORD_MODES = ["fixed", "generated", "prompt"]
ON_CONFLICT = ["block", "use_policy"]
FILESYSTEMS = ["exFAT", "NTFS", "FAT32"]
PARTITION_STYLES = ["GPT", "MBR"]
DEFAULT_VOLUME_LABEL = "SECUREUSB"

DEFAULT_COLORS = {
    "EMPTY": "#6b7280",        # grey
    "SCANNING": "#2563eb",     # blue
    "NEEDS_WORK": "#f59e0b",   # amber
    "PROCESSING": "#7c3aed",   # purple
    "DONE": "#16a34a",         # green
    "ALREADY_OK": "#16a34a",   # green
    "REJECTED": "#dc2626",     # red
    "FAILED": "#dc2626",       # red
}


APP_ROOT = Path(__file__).resolve().parent.parent      # the folder that holds run_station.py
_DATA_DIR: Path | None = None
DATA_DIR_FALLBACK = False                               # True if the app folder was not writable


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=d):
            pass
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """Where settings and records live by default: <app folder>/data (or $USBLOCKBOX_HOME/data).

    If that location is not writable (for example the app sits in Program Files) the per-user
    profile folder is used instead and DATA_DIR_FALLBACK is set so the UI can say so.
    """
    global _DATA_DIR, DATA_DIR_FALLBACK
    if _DATA_DIR is None:
        home = Path(os.environ["USBLOCKBOX_HOME"]) if os.environ.get("USBLOCKBOX_HOME") else APP_ROOT
        d = home / "data"
        if not _writable(d):
            base = os.environ.get("APPDATA") if IS_WINDOWS else None
            d = (Path(base) if base else Path.home() / ".config") / APP_SLUG
            d.mkdir(parents=True, exist_ok=True)
            DATA_DIR_FALLBACK = True
        _DATA_DIR = d
    return _DATA_DIR


def settings_dir() -> Path:
    return data_dir()


def default_csv_dir() -> str:
    return str(data_dir() / "records")


def default_pdf_dir() -> str:
    return str(data_dir() / "records" / "pdf")


# ---------------------------------------------------------------- secret protection
def _dpapi(data: bytes, protect: bool, machine: bool = False) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    flags = 0x4 if machine else 0          # 0x4 = CRYPTPROTECT_LOCAL_MACHINE (any account on this PC)
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, None, None, None, flags, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect_secret(plain: str) -> str:
    if not plain:
        return ""
    if IS_WINDOWS:
        return "dpapi-u:" + base64.b64encode(_dpapi(plain.encode("utf-8"), True)).decode()
    # Non-Windows (development/simulation only): obfuscation, NOT protection.
    return "plain:" + base64.b64encode(plain.encode("utf-8")).decode()


def unprotect_secret(blob: str) -> str:
    if not blob:
        return ""
    kind, _, payload = blob.partition(":")
    raw = base64.b64decode(payload)
    if kind.startswith("dpapi") and not IS_WINDOWS:
        raise ValueError("A Windows-protected secret cannot be opened on this platform.")
    if kind == "dpapi-u":
        return _dpapi(raw, False).decode("utf-8")
    if kind == "dpapi":                       # legacy blobs written with machine scope
        return _dpapi(raw, False, machine=True).decode("utf-8")
    if kind == "plain":
        return raw.decode("utf-8")
    raise ValueError("unknown secret format")


# ---------------------------------------------------------------- settings
@dataclass
class Settings:
    # Safety / mode
    dry_run: bool = True                    # scan + plan only, never erase. Turning it off is "real mode"
    allow_fixed_disks: bool = False         # USB-attached fixed (HDD/SSD) disks
    min_size_gb: float = 1.0
    max_size_gb: float = 512.0
    require_confirm_if_content: bool = True
    operator: str = ""                      # optional override; by default the signed-in Windows user is used
    schema_version: int = 2                 # older files (before 2) are started in dry-run once

    # Station
    hidden_ports: list = field(default_factory=list)   # port keys the operator chose not to show (Settings > Ports)
    port_names: dict = field(default_factory=dict)     # port key -> the operator's label
    simulator_ports: int = 4                # number of virtual ports in Simulator mode (real ports are detected)

    # Sanitization
    overwrite_passes: int = 3               # 0 = clear-disk only
    zero_edge_mb: int = 1
    verify_readback: bool = True
    capacity_test: bool = False             # slow; detects counterfeit-capacity drives

    # Encryption
    encryption_method: str = "XtsAes256"
    filesystem: str = "exFAT"
    partition_style: str = "GPT"
    volume_label: str = DEFAULT_VOLUME_LABEL
    full_volume_encryption: bool = True     # never used-space-only (see design notes)
    on_policy_method_conflict: str = "block"  # "block" or "use_policy"
    # Skip re-provisioning only if encrypted OK + empty + clean layout + serial in records as
    # provisioned here. OFF by default: an "empty" drive can still hold recoverable deleted data
    # (encrypted under the same shared password), so it should normally be re-wiped.
    reuse_compliant_drives: bool = False

    # Passwords
    password_mode: str = "fixed"
    fixed_password_enc: str = ""            # DPAPI blob
    generated_length: int = 24
    password_profile_id: str = "default"    # reserved for future per-agency profiles
    min_password_length: int = 15           # app-side floor; check it against your own password policy

    # Records
    csv_dir: str = ""                       # blank = <app folder>/data/records
    pdf_dir: str = ""                       # blank = <app folder>/data/records/pdf
    include_secrets_csv: bool = False       # the CSV is a master list: off unless you need it
    include_secrets_pdf: bool = True        # one PDF per drive, so the recovery key can travel with the drive's file

    # Updates
    check_updates: bool = True              # ask GitHub for a newer release at start-up (no other data is sent)

    # Display
    colors: dict = field(default_factory=lambda: dict(DEFAULT_COLORS))

    # --- secret helpers
    def set_fixed_password(self, plain: str) -> None:
        self.fixed_password_enc = protect_secret(plain)

    def get_fixed_password(self) -> str:
        """Decrypted fixed password, or "" if none is set or it cannot be decrypted by this account."""
        try:
            return unprotect_secret(self.fixed_password_enc)
        except (OSError, ValueError):
            return ""

    # --- persistence
    def resolved_csv_dir(self) -> str:
        return self.csv_dir or default_csv_dir()

    def resolved_pdf_dir(self) -> str:
        return self.pdf_dir or default_pdf_dir()

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or settings_dir() / "settings.json"
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("settings file is not a JSON object")
            except (OSError, ValueError):
                try:                                    # keep the unreadable file instead of overwriting it
                    path.replace(path.with_suffix(".json.bad"))
                except OSError:
                    pass
                data = {}
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
            if not isinstance(data.get("schema_version"), int) or data["schema_version"] < 2:
                s.dry_run = True                         # a file from an older version never starts in erase mode
                s.schema_version = 2
        s.validate()
        # blank folders mean "the default under the app folder"; resolve them for use at run time
        if not s.csv_dir:
            s.csv_dir = default_csv_dir()
        if not s.pdf_dir:
            s.pdf_dir = default_pdf_dir()
        return s

    def validate(self) -> None:
        """Clamp every value to what the app accepts. settings.json is just a file on disk, so nothing
        read from it is trusted: several of these values end up inside PowerShell commands."""
        def pick(v, allowed, default):
            return v if isinstance(v, str) and v in allowed else default

        def num(v, lo, hi, default, kind=float):
            try:
                x = kind(v)
            except (TypeError, ValueError):
                return default
            return min(max(x, lo), hi)

        def flag(v, default):
            return v if isinstance(v, bool) else default

        d = Settings()
        self.dry_run = flag(self.dry_run, True)
        self.allow_fixed_disks = flag(self.allow_fixed_disks, False)
        self.require_confirm_if_content = flag(self.require_confirm_if_content, True)
        self.verify_readback = flag(self.verify_readback, True)
        self.capacity_test = flag(self.capacity_test, False)
        self.full_volume_encryption = flag(self.full_volume_encryption, True)
        self.reuse_compliant_drives = flag(self.reuse_compliant_drives, False)
        self.include_secrets_csv = flag(self.include_secrets_csv, d.include_secrets_csv)
        self.include_secrets_pdf = flag(self.include_secrets_pdf, d.include_secrets_pdf)
        self.check_updates = flag(self.check_updates, True)
        self.encryption_method = pick(self.encryption_method, ENCRYPTION_METHODS, d.encryption_method)
        self.filesystem = pick(self.filesystem, FILESYSTEMS, d.filesystem)
        self.partition_style = pick(self.partition_style, PARTITION_STYLES, d.partition_style)
        self.password_mode = pick(self.password_mode, PASSWORD_MODES, d.password_mode)
        self.on_policy_method_conflict = pick(self.on_policy_method_conflict, ON_CONFLICT, d.on_policy_method_conflict)
        def clean_key(k):
            return k.strip().lower()[:300] if isinstance(k, str) and k.strip() else ""

        self.hidden_ports = sorted({clean_key(k) for k in (self.hidden_ports if isinstance(self.hidden_ports, list) else [])
                                    if clean_key(k)})[:200]
        names = {}
        for k, v in (self.port_names.items() if isinstance(self.port_names, dict) else []):
            k = clean_key(k)
            v = "".join(c for c in v if c.isprintable() and c not in "<>&\"'").strip()[:24] if isinstance(v, str) else ""
            if k and v and len(names) < 200:
                names[k] = v
        self.port_names = names
        self.simulator_ports = num(self.simulator_ports, 1, 16, d.simulator_ports, int)
        self.overwrite_passes = num(self.overwrite_passes, 0, 7, d.overwrite_passes, int)
        self.zero_edge_mb = num(self.zero_edge_mb, 1, 64, d.zero_edge_mb, int)
        self.generated_length = num(self.generated_length, 12, 64, d.generated_length, int)
        self.min_password_length = num(self.min_password_length, 8, 64, d.min_password_length, int)
        self.min_size_gb = num(self.min_size_gb, 0.1, 4096, d.min_size_gb)
        self.max_size_gb = num(self.max_size_gb, 1, 8192, d.max_size_gb)
        if self.min_size_gb > self.max_size_gb:
            self.min_size_gb, self.max_size_gb = d.min_size_gb, d.max_size_gb
        label = re.sub(r"[^A-Za-z0-9_ -]", "", str(self.volume_label or ""))[:11].strip()
        self.volume_label = label or DEFAULT_VOLUME_LABEL
        for name in ("operator", "csv_dir", "pdf_dir", "password_profile_id", "fixed_password_enc"):
            if not isinstance(getattr(self, name), str):
                setattr(self, name, getattr(d, name))
        self.operator = self.operator[:80]
        merged = dict(DEFAULT_COLORS)
        if isinstance(self.colors, dict):
            merged.update({k: v for k, v in self.colors.items()
                           if k in DEFAULT_COLORS and isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)})
        self.colors = merged

    def save(self, path: Path | None = None) -> None:
        """Write atomically. Folders left at their default are stored blank so the app folder can move."""
        path = path or settings_dir() / "settings.json"
        data = asdict(self)
        if data["csv_dir"] == default_csv_dir():
            data["csv_dir"] = ""
        if data["pdf_dir"] == default_pdf_dir():
            data["pdf_dir"] = ""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def snapshot_hash(self) -> str:
        """Hash of non-secret settings, recorded per run so a record shows which config produced it."""
        d = asdict(self)
        d.pop("fixed_password_enc", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]
