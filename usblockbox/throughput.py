"""Measures how fast each drive being processed really moves data, and which USB links it shares. Read-only: it only
writes lines to the log, so a slow batch can be explained (a drive that is slow itself, or several drives sharing one USB
link) and later estimates can be checked against real numbers. It never changes what the app does.

 * Rates: Windows' PhysicalDisk performance counters (disk read and write bytes per second), read through PowerShell
   `Get-Counter` in a background thread. A failure just logs once; it never affects processing.
 * Links: each drive's own port speed and the uplink speed of every hub between it and the computer, from the USB hub
   layout the app already reads. Drives behind the same hub uplink share that link's bandwidth.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Callable, Optional

from . import diag, usbports

SAMPLE_SECONDS = 60.0           # between log lines
FIRST_SAMPLE_SECONDS = 20.0     # first line soon after processing starts

_COUNTER_SCRIPT = r"""
$ErrorActionPreference='Stop'
$c=@(Get-Counter -Counter '\PhysicalDisk(*)\Disk Read Bytes/sec','\PhysicalDisk(*)\Disk Write Bytes/sec' -SampleInterval 2 -MaxSamples 2)
$rows=@{}
foreach($s in $c[-1].CounterSamples){
  if($s.InstanceName -match '^(\d+)(\s|$)'){
    $n=[int]$matches[1]
    if(-not $rows.ContainsKey($n)){ $rows[$n]=[pscustomobject]@{Disk=$n;Read=0.0;Write=0.0} }
    if($s.Path -like '*read bytes/sec'){ $rows[$n].Read=[double]$s.CookedValue } else { $rows[$n].Write=[double]$s.CookedValue }
  }
}
ConvertTo-Json -InputObject @($rows.Values) -Depth 3
"""

_TRAIL_PORT = re.compile(r"#USB\(\d+\)$", re.I)


def parse_rates(text: str) -> dict[int, tuple[float, float]]:
    """{disk number: (read bytes/s, write bytes/s)} from the counter script's JSON. Bad input gives {}."""
    try:
        rows = json.loads(text) if (text or "").strip() else []
    except ValueError:
        return {}
    out: dict[int, tuple[float, float]] = {}
    for r in rows if isinstance(rows, list) else [rows]:
        try:
            out[int(r["Disk"])] = (max(float(r.get("Read") or 0), 0.0), max(float(r.get("Write") or 0), 0.0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parent_path(path: str) -> str:
    """The location path of the hub a port path belongs to ("<hub>#USB(n)" -> "<hub>"); "" when there is none."""
    p = (path or "").strip()
    return _TRAIL_PORT.sub("", p) if _TRAIL_PORT.search(p) else ""


def mbps(bytes_per_second: float) -> str:
    return "%.1f MB/s" % (bytes_per_second / 1e6)


def build_report(rates: dict[int, tuple[float, float]], drives: dict[int, dict]) -> list[str]:
    """Log lines. `drives` maps disk number -> {"label": str, "chain": [{"path", "speed", "kind"}]}, where the chain
    runs from the drive's own port outward ("drive" first, then each "hub" uplink)."""
    lines: list[str] = []
    groups: dict[str, dict] = {}
    for n, info in sorted(drives.items()):
        rd, wr = rates.get(n, (0.0, 0.0))
        chain = info.get("chain") or []
        own = next((c for c in chain if c.get("kind") == "drive"), None)
        speed = usbports.speed_name(own["speed"]) if own and own.get("speed") is not None else "speed unknown"
        lines.append("Throughput disk %d (%s): read %s, write %s; own port %s" % (n, info.get("label", "?"), mbps(rd),
                                                                                 mbps(wr), speed))
        for c in chain:
            if c.get("kind") != "hub":
                continue
            g = groups.setdefault(c["path"], {"speed": c.get("speed"), "disks": [], "bytes": 0.0})
            g["disks"].append(n)
            g["bytes"] += rd + wr
    for path, g in sorted(groups.items()):
        sp = usbports.speed_name(g["speed"]) if g["speed"] is not None else "speed unknown"
        lines.append("Throughput link %s (%s): %d active drive(s) %s, together %s" % (
            path, sp, len(g["disks"]), sorted(g["disks"]), mbps(g["bytes"])))
    return lines


class ThroughputMonitor:
    """Samples while at least one disk is being processed, and stops by itself when none is."""

    def __init__(self, run_ps: Callable[..., str], held: Callable[[], set], describe: Callable[[int], Optional[dict]],
                 interval: float = SAMPLE_SECONDS, first: float = FIRST_SAMPLE_SECONDS):
        self._run_ps, self._held, self._describe = run_ps, held, describe
        self.interval, self.first = interval, first
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._warned = False

    def ensure_running(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="throughput", daemon=True)
            self._thread.start()

    def poke(self) -> None:
        """A disk was released: let the loop notice that nothing is running any more and stop."""
        self._wake.set()

    def _loop(self) -> None:
        wait = self.first
        while True:
            self._wake.wait(wait)
            woken = self._wake.is_set()
            self._wake.clear()
            if not self._held():
                return
            if woken:                                  # something was released but others still run: keep the schedule
                continue
            self.sample_once()
            wait = self.interval

    def sample_once(self) -> list[str]:
        lines: list[str] = []
        try:
            held = sorted(self._held())
            rates = parse_rates(self._run_ps(_COUNTER_SCRIPT, None, 60, "sample disk throughput"))
            drives = {n: d for n in held for d in [self._describe(n)] if d}
            lines = build_report(rates, drives)
            if held and not rates and not self._warned:
                self._warned = True
                diag.log.info("Throughput: Windows returned no disk counters (is the PhysicalDisk counter set available?)")
        except Exception as e:   # noqa: BLE001 - measuring must never disturb processing
            if not self._warned:
                self._warned = True
                diag.log.info("Throughput could not be measured: %s: %s", type(e).__name__, e)
            return []
        for line in lines:
            diag.log.info(line)
        return lines
