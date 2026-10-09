"""How drives that share a USB link share its bandwidth, and how long each will take as drives finish.

Pure arithmetic (no Qt, no Windows), so it can be tested with made-up numbers. All rates are bytes per second.

 * A link is anything with a bandwidth limit that several drives sit behind: a hub's uplink, or one drive's own port.
 * Each drive can go no faster than its own top speed ("solo") and no faster than every link it sits behind allows.
 * The links are shared fairly (progressive filling: everyone speeds up equally until a link is full or a drive is at its
   own top speed). When a drive finishes, the rest speed up, and `forecast` follows that step by step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable, Optional

# What each USB speed really carries, bytes per second. Rules of thumb (not measured on this PC): the 480 Mbit/s of USB 2.0
# gives about 35 MB/s of file data; USB 3 (5 Gbit/s) a few hundred MB/s.
SPEED_CAPACITY = {0: 0.15e6, 1: 1.0e6, 2: 35e6}
SUPER_CAPACITY = 400e6
NO_LIMIT = 1e12


def capacity(speed: Optional[int]) -> Optional[float]:
    """Bytes per second a link of this USB speed (0 low, 1 full, 2 high, 3+ super) carries; None when the speed is unknown."""
    if speed is None or speed < 0:
        return None
    return SUPER_CAPACITY if speed >= 3 else SPEED_CAPACITY.get(int(speed))


@dataclass
class Job:
    key: Hashable
    remaining: float                 # bytes of work left
    solo: float                      # bytes/s this drive manages alone
    links: list = field(default_factory=list)    # [(link name, capacity bytes/s)] shared with other drives


def allocate(jobs: list[Job]) -> dict:
    """Fair rate for every job right now. A job without work, or with no speed, gets 0."""
    rate = {j.key: 0.0 for j in jobs}
    active = [j for j in jobs if j.remaining > 0 and j.solo > 0]
    left: dict[str, float] = {}
    for j in active:
        for name, cap in j.links:
            left.setdefault(name, float(cap))
    eps = 1e-3
    for _ in range(2 * len(active) + len(left) + 5):
        if not active:
            break
        users: dict[str, int] = {}
        for j in active:
            for name, _cap in j.links:
                users[name] = users.get(name, 0) + 1
        step = min([j.solo - rate[j.key] for j in active] + [left[n] / c for n, c in users.items()])
        step = max(step, 0.0)
        for j in active:
            rate[j.key] += step
        for n, c in users.items():
            left[n] -= step * c
        full = {n for n in users if left[n] <= eps}
        active = [j for j in active if j.solo - rate[j.key] > eps and not any(n in full for n, _c in j.links)]
    return rate


def forecast(jobs: list[Job], max_rounds: int = 64) -> dict:
    """Seconds until each job's work is done, with the others speeding up as jobs finish. None = could not be worked out
    (no speed). Jobs that have no work left get 0."""
    rem = {j.key: max(float(j.remaining), 0.0) for j in jobs}
    start = dict(rem)
    done = {k: 0.0 for k, v in rem.items() if v <= 0}
    live = [j for j in jobs if rem[j.key] > 0]
    t = 0.0
    for _ in range(max_rounds):
        if not live:
            break
        rates = allocate([Job(j.key, rem[j.key], j.solo, j.links) for j in live])
        times = [rem[j.key] / rates[j.key] for j in live if rates.get(j.key, 0.0) > 0]
        if not times:
            break
        dt = min(times)
        t += dt
        nxt = []
        for j in live:
            rem[j.key] -= rates.get(j.key, 0.0) * dt
            if rem[j.key] <= max(1.0, 1e-9 * start[j.key]):
                done[j.key] = t
            else:
                nxt.append(j)
        live = nxt
    return {j.key: done.get(j.key) for j in jobs}
