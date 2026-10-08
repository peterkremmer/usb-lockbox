import json

from usblockbox import eta
from usblockbox.eta import Timings, attention, fmt_duration, remaining, step_key

PLAN = ["Clear partitions and boot records", "Zero disk edges", "Overwrite (1 pass)", "Partition and format",
        "Enable BitLocker", "Encrypt", "Verify", "Lock and finish"]


def test_step_keys():
    assert [step_key(x) for x in PLAN] == ["clear", "zero", "overwrite", "partition", "bitlocker", "encrypt", "verify", "finish"]


def test_fmt_duration():
    assert fmt_duration(20) == "under 1 min" and fmt_duration(12 * 60) == "12 min"
    assert fmt_duration(3600) == "1 h" and fmt_duration(3900) == "1 h 05 min"


def test_first_run_uses_rough_guesses_and_says_so():
    secs, measured = remaining(PLAN, PLAN[0], 0.0, 0.0, 32.0, 1, Timings())
    assert not measured and secs > 32 * (40 + 30)            # overwrite + encrypt guesses dominate


def test_live_pace_replaces_the_guess_for_the_running_step():
    t = Timings()
    secs, _ = remaining(PLAN, "Encrypt", 0.5, 600.0, 32.0, 1, t)          # 10 min for the first half
    assert 600 + 20 <= secs <= 600 + 120                                   # 10 more min + verify/lock guesses


def test_measured_timings_make_the_estimate_measured_and_persist(tmp_path):
    p = tmp_path / "timings.json"
    t = Timings(p)
    for k, v in (("clear", 5), ("zero", 3), ("overwrite", 10.0), ("partition", 20), ("bitlocker", 9),
                 ("encrypt", 8.0), ("verify", 6), ("finish", 2)):
        t.record(k, v)
    secs, measured = remaining(PLAN, PLAN[0], 0.0, 0.0, 10.0, 1, Timings(p))   # reloaded from disk
    assert measured and abs(secs - (5 + 3 + 100 + 20 + 9 + 80 + 6 + 2)) < 1e-6
    t.record("encrypt", 12.0)
    assert Timings(p).get("encrypt") == 10.0                                    # averaged, not replaced
    p.write_text("not json")
    assert Timings(p).data == {}                                                 # a damaged file is ignored


def test_unknown_step_gives_no_estimate():
    assert remaining(PLAN, "Starting", 0.0, 0.0, 32.0, 1, Timings()) is None


def _att(**kw):
    base = dict(now=10_000.0, last_progress_at=10_000.0, stall_seconds=900, key="encrypt", size_gb=32.0, passes=1,
                step_frac=0.5, step_elapsed=300.0, started_at=9_000.0, expected_total=None, timings=Timings())
    base.update(kw)
    return attention(**base)


def test_attention_quiet_when_healthy():
    assert _att() == ""


def test_attention_flags_a_stall_but_not_a_short_pause():
    assert "No progress for 16 min" in _att(last_progress_at=10_000 - 16 * 60)
    assert _att(last_progress_at=10_000 - 5 * 60) == ""


def test_attention_flags_a_drive_much_slower_than_usual():
    t = Timings()
    for _ in range(3):
        t.record("encrypt", 10.0)                                # usually 10 s per GB, measured three times
    msg = _att(timings=t, step_frac=0.1, step_elapsed=1200.0)    # 1200 s for 3.2 GB = 375 s/GB
    assert "slower than usual" in msg
    assert _att(timings=t, step_frac=0.5, step_elapsed=200.0) == ""


def test_no_slow_warning_from_a_single_measured_run():
    t = Timings(); t.record("encrypt", 1.7)                      # one earlier run, maybe a nearly empty drive
    assert t.count("encrypt") == 1
    assert _att(timings=t, step_frac=0.5, step_elapsed=1500.0) == ""     # 94 s/GB would be 55x "slower" than that one run
    for _ in range(2):
        t.record("encrypt", 1.7)
    assert "slower than usual" in _att(timings=t, step_frac=0.5, step_elapsed=1500.0)


def test_slow_warning_needs_a_big_gap():
    t = Timings()
    for _ in range(3):
        t.record("encrypt", 10.0)
    assert _att(timings=t, step_frac=0.5, step_elapsed=800.0) == ""      # 50 s/GB = 5x exactly: not past the cutoff
    assert "slower than usual" in _att(timings=t, step_frac=0.5, step_elapsed=900.0)


def test_attention_flags_overdue_only_against_a_known_expectation():
    assert "longer than expected" in _att(started_at=10_000 - 3 * 3600, expected_total=3600.0)
    assert _att(started_at=10_000 - 3 * 3600, expected_total=None) == ""


def test_fmt_ago():
    assert eta.fmt_ago(5) == "just now" and eta.fmt_ago(125) == "2 min ago" and eta.fmt_ago(7200) == "2 h ago"
