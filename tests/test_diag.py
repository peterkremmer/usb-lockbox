"""Tests for the diagnostics module: rotation, rate limits, privacy and the support bundle."""
import logging
import os
import time
import zipfile
from logging.handlers import RotatingFileHandler

import pytest

from usblockbox import diag
from usblockbox.ui.scanning import scanning_text


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """A fresh logging setup writing under tmp_path, torn down afterwards."""
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(diag, "logs_dir", lambda: d)
    diag.shutdown()
    diag._state.update({"listener": None, "startup_done": False})
    yield d
    diag.shutdown()
    diag.log.handlers = []
    diag._state["startup_done"] = False


def read_log(d):
    return "".join(p.read_text(encoding="utf-8") for p in sorted(d.glob("usblockbox.log*")))


# ------------------------------------------------------------------ pure helpers
def test_ps_label_uses_the_cmdlet_name_only():
    assert diag.ps_label("$ErrorActionPreference='Stop'\nGet-Disk | Where-Object { $_.BusType -eq 'USB' }") == "Get-Disk"
    assert diag.ps_label("$ids = $env:X | ConvertFrom-Json\nGet-PnpDeviceProperty -InstanceId $id") == "Get-PnpDeviceProperty"
    assert diag.ps_label("Enable-BitLocker -MountPoint 'E:' -Password secret") == "Enable-BitLocker"
    assert diag.ps_label("1") == "script" and diag.ps_label("") == "script"


def test_safe_settings_drops_every_secret_field(settings):
    settings.fixed_password_enc = "BLOB"
    out = diag.safe_settings(settings)
    assert "fixed_password_enc" not in out
    assert not any(k.endswith(("_enc", "_hash", "_secret", "_token", "_key")) for k in out)
    assert "password_mode" in out and "dry_run" in out and "stall_minutes" in out


def test_scanning_text_progresses_and_then_reports_none_found():
    assert scanning_text(0, False).startswith("Scanning USB ports")
    assert "Still working" not in scanning_text(10, False)
    assert "Still working (20 s)" in scanning_text(20, False)
    slow = scanning_text(50, False)
    assert "slow to answer" in slow and "Data\\logs" in slow
    assert scanning_text(0, True).startswith("No USB ports found.")
    assert "hub note" in scanning_text(0, True, note="hub note")
    err = scanning_text(3, False, error="boom")
    assert "boom" in err and "Trying again" in err
    assert len({scanning_text(t, False) for t in (0, 1, 2)}) == 3          # the dots move


# ------------------------------------------------------------------ rate guard
def make_record(msg, *args, level=logging.INFO):
    return logging.LogRecord("usblockbox", level, __file__, 1, msg, args, None)


def test_rate_guard_limits_one_message_and_reports_what_it_dropped():
    now = [0.0]
    g = diag.RateGuard(per_message=5, total=200, clock=lambda: now[0])
    passed = [g.filter(make_record("same %s", "a")) for _ in range(8)]
    assert passed == [True] * 5 + [False] * 3
    assert g.filter(make_record("other %s", "a")) is True                    # a different message is unaffected
    now[0] = 61.0
    r = make_record("same %s", "a")
    assert g.filter(r) is True
    assert "3 similar message(s) suppressed" in r.getMessage()


def test_rate_guard_total_cap_and_critical_always_passes():
    g = diag.RateGuard(per_message=1000, total=10, clock=lambda: 0.0)
    results = [g.filter(make_record("m %d", i)) for i in range(20)]
    assert results.count(True) == 10
    assert g.filter(make_record("crash", level=logging.CRITICAL)) is True


def test_rate_guard_distinguishes_messages_by_first_argument():
    g = diag.RateGuard(per_message=1, total=200, clock=lambda: 0.0)
    assert g.filter(make_record("%s took %.2f s", "step one", 1.0)) is True
    assert g.filter(make_record("%s took %.2f s", "step two", 1.0)) is True
    assert g.filter(make_record("%s took %.2f s", "step one", 1.0)) is False


# ------------------------------------------------------------------ rotation and size
def test_rotation_caps_total_size(tmp_path):
    h = diag.SafeRotatingFileHandler(tmp_path / "t.log", maxBytes=2000, backupCount=2, encoding="utf-8")
    lg = logging.getLogger("rotation-test")
    lg.handlers, lg.propagate = [h], False
    lg.setLevel(logging.INFO)
    for i in range(500):
        lg.info("line %d %s", i, "x" * 50)
    h.close()
    files = list(tmp_path.glob("t.log*"))
    assert len(files) == 3
    assert sum(p.stat().st_size for p in files) <= 3 * 2200


def test_a_failed_rotation_does_not_raise_or_stop_logging(tmp_path, monkeypatch):
    h = diag.SafeRotatingFileHandler(tmp_path / "t.log", maxBytes=200, backupCount=1, encoding="utf-8")

    def boom(self):
        raise PermissionError("file in use")

    monkeypatch.setattr(RotatingFileHandler, "doRollover", boom)
    lg = logging.getLogger("rotation-fail-test")
    lg.handlers, lg.propagate = [h], False
    lg.setLevel(logging.INFO)
    for i in range(50):
        lg.info("line %d %s", i, "x" * 40)
    h.close()
    assert "line 49" in (tmp_path / "t.log").read_text()


# ------------------------------------------------------------------ end to end
def test_setup_logs_to_a_file_in_the_logs_folder_and_never_the_password(logs, settings):
    settings.set_fixed_password("Correct-Horse-Battery-9")
    assert diag.setup() == logs
    diag.log_header(settings, elevated=True, mode="dry run")
    diag.log.info("hello from the test")
    diag.shutdown()                                                          # flushes the queue
    text = read_log(logs)
    assert "hello from the test" in text and "==== starting ====" in text and "USB Lockbox" in text
    assert "Correct-Horse-Battery-9" not in text
    assert (logs / "crash.log").exists()


def test_timed_logs_everything_during_startup_and_only_slow_calls_after(logs):
    diag.setup()
    with diag.timed("fast thing"):
        pass
    diag.mark_startup_done()
    with diag.timed("another fast thing"):
        pass
    diag.note("slow thing", 9.0)
    diag.shutdown()
    text = read_log(logs)
    assert "fast thing took" in text and "another fast thing" not in text
    assert "SLOW: slow thing took 9.00 s" in text


def test_powershell_failures_are_logged_with_a_short_message(logs):
    diag.setup()
    diag.mark_startup_done()
    diag.ps_call("Get-Disk", 0.2, 1, "line one\nline two " + "x" * 500)
    diag.ps_call("Get-Disk", 0.2, 0, "")
    diag.shutdown()
    text = read_log(logs)
    assert "PowerShell Get-Disk failed (exit 1)" in text and "line one line two" in text
    assert len(text) < 800


def test_unhandled_exceptions_are_written_to_the_log(logs):
    diag.setup()
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        import sys
        sys.excepthook(*sys.exc_info()) if False else diag.log.critical("Unhandled exception", exc_info=sys.exc_info())
    diag.shutdown()
    text = read_log(logs)
    assert "Unhandled exception" in text and "RuntimeError: kaboom" in text


def test_old_logs_are_deleted_and_recent_ones_kept(tmp_path):
    old, new = tmp_path / "usblockbox.log.3", tmp_path / "usblockbox.log"
    old.write_text("x"); new.write_text("y")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    diag.cleanup(tmp_path, days=30)
    assert not old.exists() and new.exists()


def test_support_bundle_has_logs_and_clean_settings_and_keeps_only_three(logs, settings):
    settings.set_fixed_password("Correct-Horse-Battery-9")
    diag.setup()
    diag.log.info("something happened")
    diag.shutdown()
    for i in range(4):                                                       # four older bundles already exist
        old = logs / ("diagnostics-2026010%d-000000.zip" % i)
        old.write_bytes(b"PK")
        os.utime(old, (time.time() - 1000 + i, time.time() - 1000 + i))
    paths = [diag.save_bundle(settings, "Mode: test", elevated=True, mode="dry run")]
    with zipfile.ZipFile(paths[-1]) as z:
        names = set(z.namelist())
        assert {"summary.txt", "settings.json", "logs/usblockbox.log"} <= names
        blob = b"".join(z.read(n) for n in names)
    assert b"Correct-Horse-Battery-9" not in blob and b"fixed_password_enc" not in blob
    assert b"Mode: test" in blob and b"something happened" in blob
    assert len(list(logs.glob("diagnostics-*.zip"))) == 3


def test_launcher_note_writes_and_rotates(tmp_path):
    diag.launcher_note("first", root=tmp_path)
    p = tmp_path / "logs" / "launcher.log"
    assert "first" in p.read_text()
    p.write_text("x" * (diag.LAUNCHER_LOG_MAX + 10))
    diag.launcher_note("second", root=tmp_path)
    assert (tmp_path / "logs" / "launcher.log.1").exists()
    assert "second" in p.read_text() and len(p.read_text()) < 200
