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

# Rough first-run guesses until real rates have been measured here.
FIXED_GUESS = {"clear": 8.0, "zero": 4.0, "partition": 25.0, "bitlocker": 15.0, "verify": 15.0, "finish": 5.0}
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


def fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return "under 1 min"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


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


def remaining(plan: list[str], label: str, step_frac: float, step_elapsed: float,
              size_gb: float, passes: int, timings: Timings) -> Optional[tuple[float, bool]]:
    """Seconds left for one drive and whether the number rests on measurements. None = unknown step."""
    if label not in plan:
        return None
    i = plan.index(label)
    key = step_key(label)
    est, measured = step_estimate(key, size_gb, passes, timings)
    f = min(max(step_frac, 0.0), 1.0)
    if key in LONG_STEPS and f >= 0.02 and step_elapsed >= 20:
        cur, cur_measured = step_elapsed * (1 - f) / f, True          # the drive's own pace, right now
    else:
        cur, cur_measured = est * (1 - f), measured
    total, all_measured = cur, cur_measured
    for later in plan[i + 1:]:
        e, m = step_estimate(step_key(later), size_gb, passes, timings)
        total += e
        all_measured = all_measured and m
    return total, all_measured


def attention(*, now: float, last_progress_at: float, stall_seconds: float, key: str, size_gb: float, passes: int,
              step_frac: float, step_elapsed: float, started_at: float, expected_total: Optional[float],
              timings: Timings) -> str:
    """A short message when a drive needs a look, else ''. Never aborts anything; it only says so."""
    idle = now - last_progress_at
    if idle >= stall_seconds:
        return (f"No progress for {fmt_duration(idle)}. This drive may be failing or stuck. "
                f"Look at it when you are back; do not unplug it while it says WORKING.")
    typical = timings.get(key) if key in LONG_STEPS else None
    if typical and step_frac >= 0.05 and step_elapsed >= 120 and size_gb > 0:
        per = step_elapsed / step_frac / (size_gb * (max(passes, 1) if key == "overwrite" else 1))
        if per > 3 * typical:
            return (f"Running about {per / typical:.0f} times slower than usual. "
                    f"This drive (or its port) may be failing.")
    if expected_total and (now - started_at) > max(2 * expected_total, expected_total + 1800):
        return (f"Taking much longer than expected ({fmt_duration(now - started_at)} so far, "
                f"about {fmt_duration(expected_total)} expected). Check this drive.")
    return ""
