"""Throughput logging: parsing, link chains, and that it can never disturb processing."""
import json
import threading
import time

from usblockbox import throughput as tp
from usblockbox import usbports as u

ROOT = "PCIROOT(0)#PCI(1400)#USBROOT(0)"


def test_parse_rates_is_forgiving():
    rows = [{"Disk": 3, "Read": 1.5e6, "Write": 2.5e6}, {"Disk": "4", "Read": None, "Write": -5}, {"nodisk": 1}]
    assert tp.parse_rates(json.dumps(rows)) == {3: (1.5e6, 2.5e6), 4: (0.0, 0.0)}
    assert tp.parse_rates(json.dumps(rows[0])) == {3: (1.5e6, 2.5e6)}        # PowerShell gives a bare object for one row
    assert tp.parse_rates("") == {} and tp.parse_rates("not json") == {}


def test_parent_path():
    assert tp.parent_path(ROOT + "#USB(1)#USB(3)") == ROOT + "#USB(1)"
    assert tp.parent_path(ROOT + "#USB(2)") == ROOT
    assert tp.parent_path(ROOT) == ""


def _scanner():
    sc = u.PortScanner(lambda *a, **k: "")
    sc._hubs = [u.HubRaw("root", "r", ROOT, 4, {}, "p0"),
                u.HubRaw("h1", "i1", ROOT + "#USB(1)", 4, {}, "p1"),           # a hub on root port 1
                u.HubRaw("h2", "i2", ROOT + "#USB(1)#USB(4)", 4, {}, "p2")]    # a second chip behind it
    sc.speed_reader = lambda path, n: {"p0": 3, "p1": 2, "p2": 3}[path] if n else None
    return sc


def test_link_chain_walks_from_the_drive_out_through_every_hub():
    chain = _scanner().link_chain(ROOT + "#USB(1)#USB(4)#USB(2)")
    assert [(c["kind"], c["path"]) for c in chain] == [
        ("drive", ROOT + "#USB(1)#USB(4)#USB(2)"),
        ("hub", ROOT + "#USB(1)#USB(4)"),            # the second chip's uplink
        ("hub", ROOT + "#USB(1)")]                   # the first hub's uplink; the root hub has none
    # a port's speed is read from the hub that owns the port, so the drive's own speed comes from the second chip (p2)
    assert [c["speed"] for c in chain] == [3, 2, 3]


def test_link_chain_for_a_drive_straight_on_the_computer():
    chain = _scanner().link_chain(ROOT + "#USB(3)")
    assert [c["kind"] for c in chain] == ["drive"]


def test_link_chain_never_raises():
    sc = u.PortScanner(lambda *a, **k: "")
    sc._hubs = [u.HubRaw("root", "r", ROOT, 4, {}, "p0")]
    sc.speed_reader = lambda path, n: (_ for _ in ()).throw(OSError("no"))
    assert sc.link_chain(ROOT + "#USB(1)")[0]["speed"] is None
    assert sc.link_chain("") and sc.link_chain(None)


def test_report_groups_drives_by_shared_uplink():
    hub = ROOT + "#USB(1)"
    drives = {n: {"label": "serial S%d" % n,
                  "chain": [{"kind": "drive", "path": hub + "#USB(%d)" % n, "speed": 2}, {"kind": "hub", "path": hub, "speed": 2}]}
              for n in (3, 4)}
    drives[5] = {"label": "serial S5", "chain": [{"kind": "drive", "path": ROOT + "#USB(2)", "speed": 3}]}
    lines = tp.build_report({3: (1e6, 9e6), 4: (0.0, 10e6), 5: (0.0, 25e6)}, drives)
    assert any("disk 3" in l and "write 9.0 MB/s" in l and "USB 2.0" in l for l in lines)
    link = [l for l in lines if l.startswith("Throughput link")]
    assert len(link) == 1 and "2 active drive(s) [3, 4]" in link[0] and "together 20.0 MB/s" in link[0]


def test_monitor_logs_and_survives_every_kind_of_failure():
    def describe(n):
        return {"label": "serial S%d" % n, "chain": []}
    good = tp.ThroughputMonitor(lambda *a, **k: json.dumps([{"Disk": 2, "Read": 1e6, "Write": 3e6}]), lambda: {2}, describe)
    assert any("write 3.0 MB/s" in l for l in good.sample_once())
    bad = tp.ThroughputMonitor(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("powershell gone")), lambda: {2}, describe)
    assert bad.sample_once() == [] and bad.sample_once() == []                  # no exception, warns once
    empty = tp.ThroughputMonitor(lambda *a, **k: "", lambda: {2}, describe)
    assert empty.sample_once() and "no disk counters" not in "".join(empty.sample_once())


def test_monitor_thread_stops_when_nothing_is_running_any_more():
    held = {1}
    mon = tp.ThroughputMonitor(lambda *a, **k: "[]", lambda: set(held), lambda n: None, interval=0.05, first=0.05)
    mon.ensure_running(); mon.ensure_running()                                 # a second call does not start a second thread
    time.sleep(0.2)
    assert mon._thread.is_alive()
    held.clear(); mon.poke()
    mon._thread.join(2)
    assert not mon._thread.is_alive()
