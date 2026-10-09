"""Partition step: diskpart runs one drive at a time, and a 'not convertible' answer is retried once."""
import threading
import time

import pytest

from usblockbox.backends import windows as w
from usblockbox.backends.base import BackendError


def _backend():
    return w.WindowsBackend.__new__(w.WindowsBackend)


def test_drives_do_not_run_diskpart_at_the_same_time(monkeypatch):
    b, running, worst = _backend(), [0], [0]
    guard = threading.Lock()

    def fake(script, timeout=120):
        with guard:
            running[0] += 1
            worst[0] = max(worst[0], running[0])
        time.sleep(0.05)
        with guard:
            running[0] -= 1
        return ""

    monkeypatch.setattr(b, "_diskpart", fake)
    ts = [threading.Thread(target=b._clean_and_convert, args=(n, "GPT")) for n in (3, 4, 5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert worst[0] == 1


def test_not_convertible_is_retried_once(monkeypatch):
    b, calls = _backend(), []
    monkeypatch.setattr(w, "run_ps", lambda *a, **k: "")
    monkeypatch.setattr(w.time, "sleep", lambda s: None)

    def fake(script, timeout=120):
        calls.append(script)
        if len(calls) == 1:
            raise BackendError("diskpart failed: The specified disk is not convertible.", "DISKPART")
        return ""

    monkeypatch.setattr(b, "_diskpart", fake)
    b._clean_and_convert(4, "GPT")
    assert len(calls) == 2 and "convert gpt" in calls[0]


def test_other_errors_and_a_second_failure_still_fail(monkeypatch):
    b = _backend()
    monkeypatch.setattr(w, "run_ps", lambda *a, **k: "")
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(b, "_diskpart", lambda s, timeout=120: (_ for _ in ()).throw(BackendError("diskpart failed: boom", "DISKPART")))
    with pytest.raises(BackendError):
        b._clean_and_convert(4, "GPT")
    monkeypatch.setattr(b, "_diskpart", lambda s, timeout=120: (_ for _ in ()).throw(BackendError("diskpart failed: not convertible", "DISKPART")))
    with pytest.raises(BackendError):
        b._clean_and_convert(4, "GPT")
