"""Diagnostics: a small rotating log in <data>/logs, crash capture and a one-click support bundle.

Rules that keep logging from ever hurting the app:
  * Callers only put a record on an in-memory queue; a background thread writes it. A slow disk or a
    security product scanning the file cannot slow the window or a scan.
  * Hard size cap: usblockbox.log rotates at 1 MB and keeps 5 files; files older than 30 days are deleted at start.
  * Rate guard: one message is logged at most 5 times a minute and the whole app at most 200 lines a minute;
    what was dropped is counted and reported on the next line that is written.
  * Only events are logged, never every poll. Slow operations (5 s or more) are always logged; during the
    first scan every step is, so a slow start can be traced.
  * Nothing here may raise into the app.
  * Never logged: passwords, recovery keys, file names found on a drive.
"""
from __future__ import annotations

import atexit
import contextlib
import dataclasses
import faulthandler
import getpass
import json
import logging
import os
import platform
import queue
import re
import struct
import sys
import threading
import time
import zipfile
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Optional

LOG_NAME = "usblockbox"
MAX_BYTES = 1_000_000
BACKUPS = 4                      # usblockbox.log plus 4 older files = 5 files, about 5 MB at most
KEEP_DAYS = 30
PER_MESSAGE_PER_MIN = 5
TOTAL_PER_MIN = 200
SLOW_SECONDS = 5.0
BUNDLES_TO_KEEP = 3
LAUNCHER_LOG_MAX = 200_000

log = logging.getLogger(LOG_NAME)
logging.raiseExceptions = False          # a logging problem must never print or raise inside the app

_state: dict = {"listener": None, "dir": None, "startup_done": False, "crash_file": None}
_SECRET_FIELD = re.compile(r"(_enc|_hash|_secret|_token|_key)$|^fixed_password$", re.I)
_PS_LABEL = re.compile(r"\b(?!Convert)([A-Z][a-z]+-[A-Z][A-Za-z]+)\b")


# ---------------------------------------------------------------- pure helpers (unit-tested)
def ps_label(script: str) -> str:
    """A short, safe name for a PowerShell call: its first cmdlet (never the script text, which may hold values)."""
    m = _PS_LABEL.search(script or "")
    return m.group(1) if m else "script"


def safe_settings(settings) -> dict:
    """The settings as plain data with every secret-looking field removed."""
    try:
        d = dataclasses.asdict(settings)
    except TypeError:
        d = dict(getattr(settings, "__dict__", {}))
    return {k: v for k, v in d.items() if not _SECRET_FIELD.search(k)}


class RateGuard(logging.Filter):
    """Caps how often one message, and the app as a whole, can write to the log."""

    def __init__(self, per_message: int = PER_MESSAGE_PER_MIN, total: int = TOTAL_PER_MIN, clock=time.monotonic):
        super().__init__()
        self.per_message, self.total, self.clock = per_message, total, clock
        self._lock = threading.Lock()
        self._window = clock()
        self._count = 0
        self._dropped_total = 0
        self._msgs: dict = {}

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            with self._lock:
                now = self.clock()
                if now - self._window >= 60:
                    self._window, self._count = now, 0
                    for v in self._msgs.values():
                        v[0] = 0
                if len(self._msgs) > 500:
                    self._msgs.clear()
                first_arg = ""
                if isinstance(record.args, tuple) and record.args:
                    first_arg = str(record.args[0])[:80]
                key = (record.levelno, str(record.msg)[:200], first_arg)
                entry = self._msgs.setdefault(key, [0, 0])
                if record.levelno < logging.CRITICAL:
                    if self._count >= self.total:
                        entry[1] += 1
                        self._dropped_total += 1
                        return False
                    if entry[0] >= self.per_message:
                        entry[1] += 1
                        return False
                entry[0] += 1
                self._count += 1
                notes = []
                if entry[1]:
                    notes.append("%d similar message(s) suppressed" % entry[1])
                    entry[1] = 0
                if self._dropped_total:
                    notes.append("%d message(s) dropped by the per-minute cap" % self._dropped_total)
                    self._dropped_total = 0
                if notes:
                    record.msg = "%s  [%s]" % (record.getMessage(), "; ".join(notes))
                    record.args = ()
                return True
        except Exception:   # noqa: BLE001 - never let the guard break logging
            return True


class SafeRotatingFileHandler(RotatingFileHandler):
    """Keeps writing if a rotation fails (for example another program holds the old file open)."""
    _retry_at = 0.0

    def shouldRollover(self, record):
        if time.monotonic() < self._retry_at:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + 300
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    pass


# ---------------------------------------------------------------- setup
def logs_dir() -> Path:
    from .config import data_dir
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup(d: Path, days: int = KEEP_DAYS) -> None:
    """Delete log and bundle files older than `days`."""
    cutoff = time.time() - days * 86400
    for p in d.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff and (".log" in p.name or p.suffix == ".zip"):
                p.unlink()
        except OSError:
            pass


def setup() -> Optional[Path]:
    """Start logging. Returns the logs folder, or None if it cannot be written (the app then runs without a log)."""
    if _state["listener"] is not None:
        return _state["dir"]
    try:
        d = logs_dir()
        cleanup(d)
        handler = SafeRotatingFileHandler(d / "usblockbox.log", maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                          encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        q: queue.Queue = queue.Queue(maxsize=5000)
        qh = QueueHandler(q)
        qh.addFilter(RateGuard())
        log.handlers = [qh]
        log.propagate = False
        log.setLevel(logging.DEBUG if os.environ.get("USBLOCKBOX_LOG", "").lower() == "debug" else logging.INFO)
        listener = QueueListener(q, handler, respect_handler_level=True)
        listener.start()
        _state["listener"], _state["dir"] = listener, d
        atexit.register(shutdown)
        _enable_crash_file(d)
        _install_hooks()
        return d
    except Exception:   # noqa: BLE001
        return None


def shutdown() -> None:
    lst = _state.get("listener")
    if lst is not None:
        try:
            lst.stop()
        except Exception:   # noqa: BLE001
            pass
        _state["listener"] = None


def _enable_crash_file(d: Path) -> None:
    """Hard crashes (no Python traceback) leave a stack dump in crash.log."""
    try:
        p = d / "crash.log"
        if p.exists() and p.stat().st_size > 256_000:
            p.replace(d / "crash.log.1")
        f = open(p, "a", buffering=1, encoding="utf-8")
        f.write("--- started %s ---\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        faulthandler.enable(file=f, all_threads=True)
        _state["crash_file"] = f
    except Exception:   # noqa: BLE001
        pass


def _install_hooks() -> None:
    def hook(exc_type, exc, tb):
        try:
            log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        except Exception:   # noqa: BLE001
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:   # noqa: BLE001
            pass

    def thread_hook(args):
        try:
            log.critical("Unhandled exception in thread %s", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:   # noqa: BLE001
            pass

    sys.excepthook = hook
    threading.excepthook = thread_hook


def install_qt_handler() -> None:
    """Qt warnings (for example a missing platform plugin) go to the log too."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def handler(mode, _context, message):
            level = {QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
                     QtMsgType.QtFatalMsg: logging.CRITICAL}.get(mode)
            if level is not None:
                log.log(level, "Qt: %s", message)

        qInstallMessageHandler(handler)
    except Exception:   # noqa: BLE001
        pass


# ---------------------------------------------------------------- what to write
def environment_lines(settings=None, elevated: Optional[bool] = None, mode: str = "") -> list[str]:
    from . import __version__
    from .config import APP_ROOT, data_dir
    lines = [
        "USB Lockbox %s" % __version__,
        "Python %s (%d-bit) at %s" % (platform.python_version(), struct.calcsize("P") * 8, sys.executable),
        "System: %s; machine %s; user %s" % (platform.platform(), platform.node(), _user()),
        "Elevated (administrator): %s; mode: %s" % ("unknown" if elevated is None else elevated, mode or "unknown"),
        "App folder: %s; data folder: %s" % (APP_ROOT, data_dir()),
    ]
    if settings is not None:
        lines.append("Settings: " + json.dumps(safe_settings(settings), default=str, sort_keys=True))
    return lines


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:   # noqa: BLE001
        return "unknown"


def log_header(settings=None, elevated: Optional[bool] = None, mode: str = "") -> None:
    try:
        log.info("==== starting ====")
        for line in environment_lines(settings, elevated, mode):
            log.info("%s", line)
    except Exception:   # noqa: BLE001
        pass


def mark_startup_done() -> None:
    _state["startup_done"] = True


def startup_done() -> bool:
    return bool(_state["startup_done"])


def reset_startup() -> None:
    """A new backend (real/simulator switch) starts a new first scan."""
    _state["startup_done"] = False


def note(label: str, seconds: float) -> None:
    """Log a duration: always during the first scan, afterwards only if it was slow."""
    try:
        if not _state["startup_done"]:
            log.info("%s took %.2f s", label, seconds)
        elif seconds >= SLOW_SECONDS:
            log.warning("SLOW: %s took %.2f s", label, seconds)
    except Exception:   # noqa: BLE001
        pass


@contextlib.contextmanager
def timed(label: str):
    """Time a block and log it with note()."""
    t = time.perf_counter()
    try:
        yield
    finally:
        note(label, time.perf_counter() - t)


def ps_call(label: str, seconds: float, returncode: int, stderr: str = "") -> None:
    try:
        if returncode != 0:
            log.warning("PowerShell %s failed (exit %s) after %.2f s: %s", label, returncode, seconds,
                        " ".join((stderr or "").split())[:300])
        elif not _state["startup_done"]:
            log.info("PowerShell %s took %.2f s", label, seconds)
        elif seconds >= SLOW_SECONDS:
            log.warning("SLOW: PowerShell %s took %.2f s", label, seconds)
    except Exception:   # noqa: BLE001
        pass


# ---------------------------------------------------------------- launcher notes (one small separate file)
def launcher_note(message: str, root: Optional[Path] = None) -> None:
    """One line in launcher.log: the launcher check and the elevation hand-over run before/outside the main log."""
    try:
        from .config import data_dir
        d = (Path(root) if root else data_dir()) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "launcher.log"
        if p.exists() and p.stat().st_size > LAUNCHER_LOG_MAX:
            p.replace(d / "launcher.log.1")
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:   # noqa: BLE001
        pass


# ---------------------------------------------------------------- support bundle
def save_bundle(settings, summary_extra: str = "", elevated: Optional[bool] = None, mode: str = "") -> Path:
    """Zip the logs, a secret-free copy of the settings and a summary into one file to send for support.

    Drive records are NOT included (they can hold passwords and recovery keys)."""
    d = logs_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = d / ("diagnostics-%s.zip" % stamp)
    summary = "\n".join(environment_lines(settings, elevated, mode)) + "\n"
    if summary_extra:
        summary += "\n" + summary_extra + "\n"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", summary)
        z.writestr("settings.json", json.dumps(safe_settings(settings), indent=2, default=str, sort_keys=True))
        for p in sorted(d.iterdir()):
            if p.is_file() and (".log" in p.name) and not p.name.endswith(".zip"):
                try:
                    z.write(p, "logs/" + p.name)
                except OSError:
                    pass
    old = sorted(d.glob("diagnostics-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in old[BUNDLES_TO_KEEP:]:
        try:
            p.unlink()
        except OSError:
            pass
    return out
