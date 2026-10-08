"""The application icon: the padlock from the README banner, shipped in usblockbox/assets (icon.ico has 16 to 256 px)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtGui import QIcon

ASSETS = Path(__file__).resolve().parent.parent / "assets"
APP_USER_MODEL_ID = "PeterKremmer.USBLockbox"     # lets the taskbar group the window under this icon, not under Python's


def icon_file() -> Optional[Path]:
    for name in ("icon.ico", "icon.png"):
        p = ASSETS / name
        if p.is_file():
            return p
    return None


def app_icon() -> QIcon:
    """The padlock icon, or an empty icon (never an error) if the file is missing."""
    p = icon_file()
    return QIcon(str(p)) if p else QIcon()


def set_taskbar_identity() -> None:
    """Windows: give the process its own identity so the taskbar shows our icon instead of python's. Call before any window."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:   # noqa: BLE001 - cosmetic only
        pass
