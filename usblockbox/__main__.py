"""Entry point:  python -m usblockbox   (add --selftest-windows / --policy-dump / --ports-dump / --startup-timing / --compare-native for read-only diagnostics)."""
from __future__ import annotations

import ctypes
import json
import sys

from . import diag
from .config import IS_WINDOWS, Settings


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin()) if IS_WINDOWS else True
    except Exception:   # noqa: BLE001
        return False


def _relaunch_elevated(argv: list[str]) -> bool:
    """Re-run this app elevated via the UAC prompt. UNTESTED here (needs a real Windows desktop)."""
    import os
    from pathlib import Path
    exe = sys.executable
    pyw = Path(exe).with_name("pythonw.exe")          # no console window
    if pyw.exists():
        exe = str(pyw)
    root = Path(__file__).resolve().parent.parent
    params = subprocess_list2cmdline(["-m", "usblockbox", "--no-elevate", *argv])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, str(root), 1)
    diag.launcher_note("Not running as administrator; asked Windows for elevation (%s)"
                       % ("accepted" if rc > 32 else "refused, code %s" % rc))
    return rc > 32


def subprocess_list2cmdline(args: list[str]) -> str:
    import subprocess
    return subprocess.list2cmdline(args)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--policy-dump" in argv:
        from . import policy
        print(json.dumps(policy.read_raw(), indent=2, default=str))
        return 0
    if "--selftest-windows" in argv:
        from .backends.windows import selftest
        return selftest()

    if "--ports-dump" in argv:
        if not IS_WINDOWS:
            print("--ports-dump needs Windows.")
            return 1
        from .backends.windows import ports_dump
        return ports_dump()

    if "--compare-native" in argv:
        if not IS_WINDOWS:
            print("--compare-native needs Windows.")
            return 1
        from .backends.windows import compare_native
        return compare_native()
    if "--startup-timing" in argv:
        if not IS_WINDOWS:
            print("--startup-timing needs Windows.")
            return 1
        from .backends.windows import startup_timing
        return startup_timing()

    from PySide6.QtWidgets import QApplication
    from .ui.controller import Controller
    from .ui.main_window import MainWindow

    from .ui.icon import app_icon, set_taskbar_identity
    set_taskbar_identity()
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon())
    from .ui.theme import apply_tooltip_style
    apply_tooltip_style()
    settings = Settings.load()

    admin = _is_admin() if IS_WINDOWS else True
    if IS_WINDOWS and not admin and "--no-elevate" not in argv and _relaunch_elevated(argv):
        return 0              # the elevated copy takes over after the UAC prompt
    diag.setup()              # after the hand-over, so only the copy that keeps running owns the log
    diag.install_qt_handler()
    diag.log_header(settings, elevated=admin, mode="dry run" if settings.dry_run else "real")

    if IS_WINDOWS:
        from .backends.windows import WindowsBackend
        backend = WindowsBackend(
            lambda: settings.get_fixed_password() if settings.password_mode == "fixed" and settings.fixed_password_enc else "",
            elevated=admin)
    else:
        from .backends.empty import NoHardwareBackend
        backend = NoHardwareBackend()

    ctl = Controller(backend, settings)
    win = MainWindow(ctl, settings)      # starts scanning USB ports and drives
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
