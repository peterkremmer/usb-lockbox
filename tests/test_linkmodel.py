"""Shared USB links: fair shares, forecasts that follow drives finishing, and the estimator pieces built on them."""
import random

import pytest

from usblockbox import eta
from usblockbox import linkmodel as lm
from usblockbox.eta import RateWindow, Timings, remaining
from usblockbox.pipeline import plan_steps
from usblockbox.config import Settings

MB, GB = 1e6, 1e9
HUB = [("hub", 35 * MB)]


def _jobs(n, work=32 * GB, solo=25 * MB, links=HUB):
    return [lm.Job(i, work, solo, list(links)) for i in range(n)]


def test_capacity_by_speed():
    assert lm.capacity(2) == 35 * MB and lm.capacity(3) == lm.SUPER_CAPACITY and lm.capacity(4) == lm.SUPER_CAPACITY
    assert lm.capacity(None) is None and lm.capacity(-1) is None


def test_four_drives_on_one_usb2_link_each_get_a_quarter():
    r = lm.allocate(_jobs(4))
    assert all(abs(v - 8.75 * MB) < 1 for v in r.values())
    assert abs(sum(r.values()) - 35 * MB) < 1                      # the link is full, never over


def test_a_lone_drive_runs_at_its_own_speed_and_two_split_the_link():
    assert abs(lm.allocate(_jobs(1))[0] - 25 * MB) < 1
    assert all(abs(v - 17.5 * MB) < 1 for v in lm.allocate(_jobs(2)).values())
    assert all(abs(v - 25 * MB) < 1 for v in lm.allocate(_jobs(4, links=[("hub", 400 * MB)])).values())   # USB 3: no sharing


def test_a_slow_drive_leaves_the_rest_to_the_others():
    jobs = _jobs(3)
    jobs[0].solo = 5 * MB                                          # a drive that is slow by itself
    r = lm.allocate(jobs)
    assert abs(r[0] - 5 * MB) < 1 and abs(r[1] - 15 * MB) < 1 and abs(r[2] - 15 * MB) < 1


def test_a_hub_behind_a_hub_limits_to_the_slowest_link():
    chain = [("chip2", 35 * MB), ("chip1", 400 * MB)]
    r = lm.allocate([lm.Job(i, GB, 25 * MB, list(chain)) for i in range(4)])
    assert all(abs(v - 8.75 * MB) < 1 for v in r.values())


def _truth(jobs, dt=1.0):
    """Step the world second by second; return when each job finishes."""
    rem = {j.key: j.remaining for j in jobs}
    out, t = {}, 0.0
    while rem and t < 10 ** 7:
        rates = lm.allocate([lm.Job(k, v, next(j for j in jobs if j.key == k).solo, next(j for j in jobs if j.key == k).links)
                             for k, v in rem.items()])
        t += dt
        for k in list(rem):
            rem[k] -= rates[k] * dt
            if rem[k] <= 0:
                out[k] = t
                del rem[k]
    return out


def test_forecast_follows_drives_finishing():
    jobs = _jobs(4)
    jobs[0].remaining, jobs[1].remaining = 8 * GB, 16 * GB          # two drives are nearly done
    f = lm.forecast(jobs)
    t = _truth(jobs)
    for k in range(4):
        assert abs(f[k] - t[k]) <= 2.0, (k, f[k], t[k])
    assert f[0] < f[1] < f[2] and f[2] == pytest.approx(f[3])
    flat = 32 * GB / (8.75 * MB)                                   # what "keep going at today's pace" would predict
    assert f[2] < flat                                              # the forecast knows the others will speed up


def test_forecast_after_a_new_drive_joins():
    jobs = _jobs(4, work=20 * GB)
    f0 = lm.forecast(jobs)
    jobs.append(lm.Job(4, 20 * GB, 25 * MB, list(HUB)))             # someone clicks Process on a fifth drive
    f1 = lm.forecast(jobs)
    assert f1[0] > f0[0]                                            # time left rises, as it should
    t = _truth(jobs)
    assert all(abs(f1[k] - t[k]) <= 2.0 for k in range(5))


def test_forecast_never_fails_on_odd_input():
    assert lm.forecast([]) == {}
    odd = [lm.Job("a", 0, 10), lm.Job("b", -5, 10), lm.Job("c", 1e9, 0), lm.Job("d", 1e9, 1e6, [("x", 0)]),
           lm.Job("e", float("inf"), 1e6)]
    out = lm.forecast(odd)
    assert out["a"] == 0 and out["b"] == 0 and out["c"] is None and out["d"] is None
    rnd = random.Random(1)
    for _ in range(200):
        jobs = [lm.Job(i, rnd.choice([0, 1e6, 5e9, 3e10]), rnd.choice([0, 1e5, 2e7, 3e8]),
                       rnd.sample([("a", 35e6), ("b", 400e6), ("c", 1e6)], rnd.randint(0, 3))) for i in range(rnd.randint(1, 8))]
        res = lm.forecast(jobs)
        assert set(res) == {j.key for j in jobs} and all(v is None or v >= 0 for v in res.values())


# ---------------------------------------------------------------- the estimator pieces
def test_rate_window_follows_a_change_in_pace():
    w = RateWindow(span=180, min_span=45)
    t, f = 0.0, 0.0
    for _ in range(40):                                             # 10 minutes slowly: 0.0005 per 15 s
        t += 15; f += 0.0005; w.add(t, f)
    slow = w.rate(t)
    for _ in range(20):                                             # then 5 minutes four times faster
        t += 15; f += 0.002; w.add(t, f)
    fast = w.rate(t)
    assert slow == pytest.approx(0.0005 / 15, rel=0.05) and fast == pytest.approx(0.002 / 15, rel=0.05)
    avg = f / t                                                     # the average since the start lags far behind
    assert avg < fast * 0.7


def test_rate_window_says_nothing_without_movement():
    w = RateWindow()
    assert w.rate(0) is None
    w.add(0, 0.1); w.add(30, 0.2)
    assert w.rate(30) is None                                       # less than 45 s of history
    for t in range(60, 400, 30):
        w.add(t, 0.2)
    assert w.rate(400) is None                                      # stalled: no rate, the stall check handles it


def test_remaining_uses_the_recent_pace_when_given():
    s = Settings(); s.overwrite_passes = 0
    plan = plan_steps(s)
    t = Timings()
    avg = remaining(plan, "Encrypt", 0.5, 3600.0, 32, 0, t)[0]                       # 1 h for the first half
    recent = remaining(plan, "Encrypt", 0.5, 3600.0, 32, 0, t, recent_rate=0.5 / 600.0)[0]    # now twice as fast
    assert recent < avg and abs((avg - recent) - (3600 - 600)) < 1e-6


def test_remaining_with_a_forecast_replaces_the_long_steps_only():
    s = Settings(); s.overwrite_passes = 3
    plan = plan_steps(s)
    t = Timings()
    base = remaining(plan, plan[0], 0.0, 0.0, 32, 3, t)[0]
    long_part = sum(eta.step_estimate(k, 32, 3, t)[0] for k in ("overwrite", "encrypt"))
    got, measured = remaining(plan, plan[0], 0.0, 0.0, 32, 3, t, long_seconds=(1000.0, True))
    assert abs(got - (base - long_part + 1000.0)) < 1e-6 and not measured    # fixed steps are still guesses
    mid = remaining(plan, "Encrypt", 0.3, 100.0, 32, 3, t, long_seconds=(500.0, True))[0]
    assert mid > 500.0                                                        # plus Verify and Lock and finish


def test_long_work_left():
    s = Settings(); s.overwrite_passes = 3
    plan = plan_steps(s)
    gb32 = 32 * GB
    assert eta.long_work_left(plan, plan[0], 0.0, gb32, 3) == [("overwrite", 96 * GB), ("encrypt", gb32)]
    over = next(x for x in plan if x.startswith("Overwrite"))
    assert eta.long_work_left(plan, over, 0.5, gb32, 3) == [("overwrite", 48 * GB), ("encrypt", gb32)]
    assert eta.long_work_left(plan, "Encrypt", 0.25, gb32, 3) == [("encrypt", 24 * GB)]
    assert eta.long_work_left(plan, "Verify", 0.0, gb32, 3) == []
    assert eta.long_work_left(plan, plan[0], 0.0, gb32, 3, used_only=True) == [("overwrite", 96 * GB)]


def test_slow_warning_is_not_raised_for_a_drive_that_only_shares_a_full_link():
    t = Timings()
    for _ in range(3):
        t.record("encrypt", 10.0)
    base = dict(now=10_000.0, last_progress_at=10_000.0, stall_seconds=900, key="encrypt", size_gb=32.0, passes=0,
                step_frac=0.5, step_elapsed=3000.0, started_at=9_000.0, expected_total=None, timings=t,
                recent_rate=1.0 / (100.0 * 32))                       # 100 s per GB: 10x the usual
    assert "slower than usual" in eta.attention(**base)
    assert "slower than usual" not in eta.attention(contention=4.0, **base)   # 4 drives sharing: 100 s/GB is within 5 x 10 x 4
    assert "slower than usual" in eta.attention(contention=1.5, **base)      # a little sharing does not excuse 10x
