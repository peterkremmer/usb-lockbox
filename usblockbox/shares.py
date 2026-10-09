"""Turns "which drives are working or waiting, and which USB links do they sit behind" into: how fast each one can go
right now, how long its long work (overwrite, encryption) will take as neighbours finish, and whether it is held back by a
shared link or by itself. Pure, so it can be tested with made-up drives. See linkmodel.py for the arithmetic."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import eta, linkmodel as lm, usbports

DRIVE_LIMITED = 0.8       # a drive running below this fraction of its fair share is limited by itself, not by the link


@dataclass
class Entry:
    index: int
    plan: list
    label: str                       # the step it is in (the first step for a drive that is waiting)
    step_frac: float
    size_bytes: float
    passes: int
    used_only: bool
    chain: list                      # [{"kind": "drive"|"hub", "path", "speed"}] from the drive's own port outward
    waiting: bool = False            # not started yet: assumed to start now
    recent_rate: Optional[float] = None    # step fraction per second over the last few minutes


@dataclass
class Share:
    rate: float = 0.0                # bytes/s it gets now
    solo: float = 0.0                # bytes/s it would manage alone
    finish: Optional[float] = None   # seconds until its long work is done, with neighbours finishing and speeding it up
    sharers: int = 0                 # other drives behind the same FULL link
    link: str = ""                   # what that link is, e.g. "USB 2.0 (high speed)"
    link_cap: float = 0.0
    factor: float = 1.0              # how many times slower than alone the link makes it (1 = not held back)
    measured: bool = False           # the numbers rest on measurements, not first-run guesses
    drive_limited: bool = False      # it is slow by itself even though its link has room


def _solo(work, timings) -> tuple[float, bool]:
    """(bytes/s alone, measured?) over a mix of long steps, from the speeds learned on this PC."""
    secs, measured = 0.0, True
    for key, w in work:
        per_gb, m = eta.step_estimate(key, 1.0, 1, timings)         # seconds per GB (per pass for overwrite)
        secs += w / 1e9 * per_gb
        measured = measured and m
    total = sum(w for _k, w in work)
    return (total / secs if secs > 0 else lm.NO_LIMIT), measured


def compute(entries: list[Entry], timings: eta.Timings) -> dict[int, Share]:
    jobs: dict[int, lm.Job] = {}
    meta: dict[int, dict] = {}
    labels: dict[str, tuple[float, str]] = {}
    for e in entries:
        work = eta.long_work_left(e.plan, e.label, e.step_frac, e.size_bytes, e.passes, e.used_only)
        total = sum(w for _k, w in work)
        if total <= 0:
            continue
        solo, measured = _solo(work, timings)
        links = []
        for c in e.chain or []:
            cap = lm.capacity(c.get("speed"))
            if cap is None:
                continue
            name = "own:%d" % e.index if c.get("kind") == "drive" else str(c.get("path"))
            links.append((name, cap))
            labels[name] = (cap, usbports.speed_name(c.get("speed")))
        observed = None
        key = eta.plan_key(e.label, e.used_only)
        if not e.waiting and e.recent_rate and e.recent_rate > 0 and key in eta.LONG_STEPS:
            observed = e.recent_rate * e.size_bytes * (max(e.passes, 1) if key == "overwrite" else 1)
        jobs[e.index] = lm.Job(e.index, total, solo, links)
        meta[e.index] = {"measured": measured, "observed": observed, "learned_solo": solo}
    if not jobs:
        return {}

    first = lm.allocate(list(jobs.values()))                       # with the speeds learned on this PC
    for i, job in jobs.items():                                    # what drives are really doing corrects the guess
        obs = meta[i]["observed"]
        if obs and obs > 0:
            if obs < DRIVE_LIMITED * first[i]:
                job.solo, meta[i]["drive_limited"] = obs, True     # slow although its link has room: the drive itself
                meta[i]["measured"] = True
            elif obs > job.solo:
                job.solo, meta[i]["measured"] = obs, True
    finish = lm.forecast(list(jobs.values()))
    second = lm.allocate(list(jobs.values()))

    users: dict[str, list[int]] = {}
    for i, job in jobs.items():
        for name, _cap in job.links:
            if not name.startswith("own:"):
                users.setdefault(name, []).append(i)
    out: dict[int, Share] = {}
    for i, job in jobs.items():
        sharers, link, cap, best = 0, "", 0.0, None
        for name, _c in job.links:
            if name.startswith("own:") or len(users.get(name, [])) < 2:
                continue
            lcap, label = labels[name]
            demand = sum(jobs[j].solo for j in users[name])
            if demand <= lcap * 1.0:                                # the link has room for everyone: no sharing problem
                continue
            share_each = lcap / len(users[name])
            if best is None or share_each < best:
                best, sharers, link, cap = share_each, len(users[name]) - 1, label, lcap
        factor = max(meta[i]["learned_solo"] / first[i], 1.0) if first[i] > 0 else 1.0
        out[i] = Share(rate=second[i], solo=job.solo, finish=finish.get(i), sharers=sharers, link=link, link_cap=cap,
                       factor=factor if sharers else 1.0, measured=bool(meta[i]["measured"]),
                       drive_limited=bool(meta[i].get("drive_limited")))
    return out
