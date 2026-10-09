"""Time-left estimates and "is this drive stuck?" checks for batches nobody is watching.

No Qt in here, so it can be tested on its own. Estimates start from rough guesses and are replaced by rates
measured on this computer (kept in a small timings.json next to the settings, so they survive restarts).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

GB = 1_000_000_000

# "Slower than usual" only means something once the usual has been measured a few times (one run can have been a
# nearly empty drive, a quick format or a different port) and when the gap is large.
MIN_RUNS_FOR_SLOW_WARNING = 3
SLOW_FACTOR = 5

# Rough first-run guesses until real rates have been measured here.
FIXED_GUESS = {"clear": 8.0, "zero": 4.0, "partition": 25.0, "bitlocker": 15.0, "verify": 15.0, "finish": 5.0,
               "encrypt_used": 20.0}   # used-space-only encryption of a freshly formatted drive: seconds, not hours
SPGB_GUESS = {"overwrite": 40.0, "encrypt": 30.0}      # seconds per GB (overwrite: per pass)
LONG_STEPS = tuple(SPGB_GUESS)                         # the steps that report their own progress


def step_key(label: str) -> str:
    l = (label or "").lower()
    for prefix, key in (("clear", "clear"), ("zero", "zero"), ("overwrite", "overwrite"), ("partition", "partition"),
                        ("enable bitlocker", "bitlocker"), ("encrypt", "encrypt"), ("verify", "verify"),
                        ("lock", "finish")):
        if l.startswith(prefix):
            return key
    return l


def plan_key(label: str, used_only: bool = False) -> str:
    """The timing key of a step. Encrypting only the used space (Settings > Encryption) takes seconds on a fresh drive,
    so it is timed as a fixed step of its own; full-volume encryption is timed per GB of the whole drive."""
    k = step_key(label)
    return "encrypt_used" if used_only and k == "encrypt" else k


HEARTBEAT_SECONDS = 120   # log a still-running step at least this often even when nothing changed (shows a stall)


def progress_due(prev_pct: int, pct: int, last_logged_at: float, now: float) -> bool:
    """Log a progress line when the step's whole-percent changed, or as a heartbeat when it has not for a while."""
    return pct != prev_pct or now - last_logged_at >= HEARTBEAT_SECONDS


def progress_line(title: str, label: str, frac: float, step_elapsed: float, size_gb: float, passes: int,
                  used_only: bool, idle: float, serial: str = "") -> str:
    """One log line: where a drive is in its current step, how long that took so far, the average speed for the long
    steps, and how long since the percentage last moved."""
    f = min(max(frac, 0.0), 1.0)
    key = plan_key(label, used_only)
    parts = [f"{title}" + (f" (serial {serial})" if serial else "") + f": {label} {f * 100:.0f}%",
             f"step running {fmt_duration(step_elapsed)}"]
    if key in LONG_STEPS and f > 0 and step_elapsed >= 10 and size_gb > 0:
        mult = max(passes, 1) if key == "overwrite" else 1
        parts.append("average %.1f MB/s" % (size_gb * 1000 * f * mult / step_elapsed))
    if idle >= 30:
        parts.append(f"percentage unchanged for {fmt_duration(idle)}")
    return ", ".join(parts)


def fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return "under 1 min"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


def fmt_ago(seconds: float) -> str:
    return "just now" if seconds < 60 else fmt_duration(seconds) + " ago"


def fmt_clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class Timings:
    """Measured seconds (fixed steps) or seconds per GB (overwrite, encrypt), averaged over recent runs."""

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else None
        self.data: dict[str, dict] = {}
        if self.path:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for k, v in (raw.items() if isinstance(raw, dict) else []):
                    if isinstance(v, dict) and isinstance(v.get("v"), (int, float)) and v["v"] > 0:
                        self.data[str(k)[:40]] = {"v": float(v["v"]), "n": int(v.get("n", 1))}
            except (OSError, ValueError):
                pass

    def get(self, key: str) -> Optional[float]:
        e = self.data.get(key)
        return e["v"] if e else None

    def count(self, key: str) -> int:
        e = self.data.get(key)
        return e["n"] if e else 0

    def record(self, key: str, value: float) -> None:
        if not (value > 0) or value > 10 ** 7:
            return
        e = self.data.get(key)
        self.data[key] = {"v": value if not e else e["v"] * 0.5 + value * 0.5, "n": (e["n"] if e else 0) + 1}
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass                                        # timings are a convenience, never a reason to fail


def step_estimate(key: str, size_gb: float, passes: int, timings: Timings) -> tuple[float, bool]:
    """(seconds, measured?) for one whole step on a drive of this size."""
    got = timings.get(key)
    if key in SPGB_GUESS:
        per = got if got is not None else SPGB_GUESS[key]
        return per * size_gb * (max(passes, 1) if key == "overwrite" else 1), got is not None
    return (got if got is not None else FIXED_GUESS.get(key, 10.0)), got is not None


class RateWindow:
    """Recent progress of the current step: how fast its fraction has been rising over the last few minutes. This follows
    changes (a neighbour finished, another drive started) that the average since the step began would hide."""

    def __init__(self, span: float = 180.0, min_span: float = 45.0):
        self.span, self.min_span = span, min_span
        self.samples: list[tuple[float, float]] = []

    def reset(self) -> None:
        self.samples = []

    def add(self, t: float, frac: float) -> None:
        if self.samples and t < self.samples[-1][0]:
            return
        self.samples.append((t, frac))
        cut = t - 2 * self.span
        while len(self.samples) > 2 and self.samples[1][0] < cut:
            self.samples.pop(0)

    def rate(self, now: float) -> Optional[float]:
        """Fraction per second over the window, or None if there is not enough movement to say."""
        if len(self.samples) < 2:
            return None
        last_t, last_f = self.samples[-1]
        inside = [s for s in self.samples if s[0] >= now - self.span]
        first_t, first_f = inside[0] if inside else self.samples[-1]
        if last_t - first_t < self.min_span or last_f <= first_f:
            return None
        return (last_f - first_f) / (last_t - first_t)


def long_work_left(plan: list[str], label: str, step_frac: float, size_bytes: float, passes: int,
                   used_only: bool = False) -> list[tuple[str, float]]:
    """[(timing key, bytes of work)] still to do in the long steps (overwrite, full-volume encryption)."""
    if label not in plan:
        return []
    f = min(max(step_frac, 0.0), 1.0)
    out: list[tuple[str, float]] = []
    for pos in range(plan.index(label), len(plan)):
        key = plan_key(plan[pos], used_only)
        if key not in LONG_STEPS:
            continue
        work = size_bytes * (max(passes, 1) if key == "overwrite" else 1)
        out.append((key, work * (1 - f) if plan[pos] == label else work))
    return out


def remaining(plan: list[str], label: str, step_frac: float, step_elapsed: float,
              size_gb: float, passes: int, timings: Timings, used_only: bool = False,
              recent_rate: Optional[float] = None,
              long_seconds: Optional[tuple[float, bool]] = None) -> Optional[tuple[float, bool]]:
    """Seconds left for one drive and whether the number rests on measurements. None = unknown step.

    recent_rate: the current step's fraction per second over the last few minutes (RateWindow); used in place of the
    average since the step began. long_seconds: (seconds, measured) for ALL the long work still to do, from the shared-link
    forecast, used in place of the per-step estimates for the long steps."""
    if label not in plan:
        return None
    i = plan.index(label)
    key = plan_key(label, used_only)
    est, measured = step_estimate(key, size_gb, passes, timings)
    f = min(max(step_frac, 0.0), 1.0)
    if long_seconds is not None and key in LONG_STEPS:
        cur, cur_measured = 0.0, True                                 # counted in the forecast below
    elif key in LONG_STEPS and recent_rate and recent_rate > 0 and f >= 0.02:
        cur, cur_measured = (1 - f) / recent_rate, True               # the drive's pace over the last few minutes
    elif key in LONG_STEPS and f >= 0.02 and step_elapsed >= 20:
        cur, cur_measured = step_elapsed * (1 - f) / f, True          # the drive's pace since this step began
    elif key in LONG_STEPS or f > 0:
        cur, cur_measured = est * (1 - f), measured
    else:                                                              # a fixed step with no progress of its own: count down
        cur, cur_measured = max(est - step_elapsed, est * 0.1), measured
    total, all_measured = cur, cur_measured
    for later in plan[i + 1:]:
        if long_seconds is not None and plan_key(later, used_only) in LONG_STEPS:
            continue
        e, m = step_estimate(plan_key(later, used_only), size_gb, passes, timings)
        total += e
        all_measured = all_measured and m
    if long_seconds is not None:
        total, all_measured = total + max(long_seconds[0], 0.0), all_measured and long_seconds[1]
    return total, all_measured


def attention(*, now: float, last_progress_at: float, stall_seconds: float, key: str, size_gb: float, passes: int,
              step_frac: float, step_elapsed: float, started_at: float, expected_total: Optional[float],
              timings: Timings, recent_rate: Optional[float] = None, contention: float = 1.0) -> str:
    """A short message when a drive needs a look, else ''. Never aborts anything; it only says so."""
    idle = now - last_progress_at
    if idle >= stall_seconds:
        return (f"No progress for {fmt_duration(idle)}. This drive may be failing or stuck. "
                f"Look at it when you are back; do not unplug it while it says WORKING.")
    typical = timings.get(key) if key in LONG_STEPS and timings.count(key) >= MIN_RUNS_FOR_SLOW_WARNING else None
    if typical and step_frac >= 0.05 and step_elapsed >= 120 and size_gb > 0:
        per = step_elapsed / step_frac / (size_gb * (max(passes, 1) if key == "overwrite" else 1))
        if recent_rate and recent_rate > 0:                    # judge the pace of the last few minutes, not the whole step
            per = 1.0 / recent_rate / (size_gb * (max(passes, 1) if key == "overwrite" else 1))
        if per > SLOW_FACTOR * typical * max(contention, 1.0):   # sharing a full link makes every drive slower: not a fault
            return (f"Running about {per / typical:.0f} times slower than usual. "
                    f"This drive (or its port) may be failing.")
    if expected_total and (now - started_at) > max(2 * expected_total, expected_total + 1800):
        return (f"Taking much longer than expected ({fmt_duration(now - started_at)} so far, "
                f"about {fmt_duration(expected_total)} expected). Check this drive.")
    return ""
