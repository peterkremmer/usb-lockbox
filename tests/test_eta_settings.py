"""Time-left and slow-drive logic must follow the settings that change the work: passes, full-volume vs used-space-only
encryption, and drive size."""
from usblockbox import eta
from usblockbox.config import Settings
from usblockbox.eta import Timings, remaining
from usblockbox.pipeline import plan_steps


def _left(settings, gb, timings=None, used_only=None):
    plan = plan_steps(settings)
    used = (not settings.full_volume_encryption) if used_only is None else used_only
    return remaining(plan, plan[0], 0.0, 0.0, gb, settings.overwrite_passes, timings or Timings(), used)


def _s(**kw):
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_plan_follows_the_overwrite_setting():
    assert [x for x in plan_steps(_s(overwrite_passes=0)) if x.startswith("Overwrite")] == []
    assert "Overwrite (1 pass)" in plan_steps(_s(overwrite_passes=1))
    assert "Overwrite (3 passes)" in plan_steps(_s(overwrite_passes=3))


def test_estimate_scales_with_passes_and_size():
    t0, t1, t3 = (_left(_s(overwrite_passes=p), 32)[0] for p in (0, 1, 3))
    per_pass = eta.SPGB_GUESS["overwrite"] * 32
    assert abs((t1 - t0) - per_pass) < 1e-6 and abs((t3 - t1) - 2 * per_pass) < 1e-6
    assert _left(_s(overwrite_passes=3), 64)[0] > _left(_s(overwrite_passes=3), 32)[0]


def test_measured_rates_are_per_pass_so_changing_passes_stays_right():
    t = Timings(); t.record("overwrite", 10.0)                     # 10 s per GB per pass, measured
    one, three = _left(_s(overwrite_passes=1), 10, t)[0], _left(_s(overwrite_passes=3), 10, t)[0]
    assert abs((three - one) - 200.0) < 1e-6


def test_encryption_method_does_not_change_the_plan_or_estimate():
    a = _left(_s(encryption_method="XtsAes256"), 32)[0]
    b = _left(_s(encryption_method="XtsAes128"), 32)[0]
    assert a == b                                                   # same steps; only the shared per-GB rate matters


def test_used_space_only_encryption_is_seconds_not_hours():
    full = _left(_s(full_volume_encryption=True, overwrite_passes=0), 256)[0]
    used = _left(_s(full_volume_encryption=False, overwrite_passes=0), 256)[0]
    assert full - used > 3600 and used < 300


def test_used_space_runs_do_not_teach_the_full_volume_rate():
    assert eta.plan_key("Encrypt", used_only=True) == "encrypt_used"
    assert eta.plan_key("Encrypt", used_only=False) == "encrypt"
    assert eta.plan_key("Overwrite (3 passes)", used_only=True) == "overwrite"
    t = Timings(); t.record("encrypt_used", 6.0)
    assert _left(_s(full_volume_encryption=False, overwrite_passes=0), 256, t)[1] is False   # other steps still guesses


def test_no_slow_warning_for_used_space_only_encryption():
    base = dict(now=10_000.0, last_progress_at=10_000.0, stall_seconds=900, size_gb=256.0, passes=0, step_frac=0.5,
                step_elapsed=3000.0, started_at=9_000.0, expected_total=None)
    t = Timings()
    for _ in range(3):
        t.record("encrypt", 1.0)
    assert eta.attention(key="encrypt", timings=t, **base) != ""               # full volume: 23 s/GB vs 1 s/GB
    assert eta.attention(key="encrypt_used", timings=t, **base) == ""          # a different kind of step: not compared


def test_fixed_step_counts_down_while_it_runs():
    plan = plan_steps(_s(overwrite_passes=0))
    t = Timings()
    start = remaining(plan, "Enable BitLocker", 0.0, 0.0, 32, 0, t)[0]
    later = remaining(plan, "Enable BitLocker", 0.0, 10.0, 32, 0, t)[0]
    assert abs((start - later) - 10.0) < 1e-6
    overrun = remaining(plan, "Enable BitLocker", 0.0, 500.0, 32, 0, t)[0]
    assert overrun > 0                                                         # never negative or zero while still running


# ---------------------------------------------------------------- progress lines in the log
def test_progress_is_logged_on_every_whole_percent_and_as_a_heartbeat():
    assert eta.progress_due(50, 51, 1000.0, 1001.0)                 # 50% -> 51%
    assert not eta.progress_due(51, 51, 1000.0, 1060.0)             # same percent, a minute later: quiet
    assert eta.progress_due(51, 51, 1000.0, 1000.0 + eta.HEARTBEAT_SECONDS)   # nothing for two minutes: say so


def test_progress_line_has_what_is_needed_to_diagnose_a_slow_step():
    line = eta.progress_line("PORT 3", "Encrypt", 0.51, 1200.0, 32.0, 3, False, 0.0, "S1")
    assert line.startswith("PORT 3 (serial S1): Encrypt 51%") and "step running 20 min" in line
    assert "average 13.6 MB/s" in line and "unchanged" not in line                # 32 GB * 0.51 / 1200 s
    stuck = eta.progress_line("PORT 3", "Encrypt", 0.51, 1200.0, 32.0, 3, False, 600.0)
    assert "percentage unchanged for 10 min" in stuck
    over = eta.progress_line("PORT 2", "Overwrite (3 passes)", 0.5, 100.0, 10.0, 3, False, 0.0)
    assert "average 150.0 MB/s" in over                                           # 10 GB * 0.5 * 3 passes in 100 s
    fixed = eta.progress_line("PORT 2", "Partition and format", 0.0, 5.0, 10.0, 3, False, 0.0)
    assert "average" not in fixed                                                 # fixed steps have no speed
    used = eta.progress_line("PORT 2", "Encrypt", 0.4, 30.0, 256.0, 0, True, 0.0)
    assert "average" not in used                                                  # used-space-only: no per-GB speed
