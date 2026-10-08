"""Native location lookup: pure logic only (the Windows calls themselves can only be tried on Windows)."""
import json
import time

import pytest

from usblockbox import usbports as u
from usblockbox.backends import windows as w


# ---------------------------------------------------------------- parsing and plausibility
def test_parse_multisz():
    raw = "PCIROOT(0)#PCI(1400)#USBROOT(0)\x00ACPI(_SB_)#ACPI(PCI0)\x00\x00".encode("utf-16-le")
    assert u.parse_multisz(raw) == ["PCIROOT(0)#PCI(1400)#USBROOT(0)", "ACPI(_SB_)#ACPI(PCI0)"]
    assert u.parse_multisz(b"") == []


ROOT = "PCIROOT(0)#PCI(1400)#USBROOT(0)"


def test_tree_is_plausible():
    paths = [ROOT, ROOT + "#USB(2)", ROOT + "#USB(2)#USB(4)", ROOT + "#USB(2)#USB(4)#USB(2)"]
    assert u.locations_plausible(paths)
    assert u.locations_plausible([p.lower() for p in paths])


@pytest.mark.parametrize("paths", [
    [],                                                  # nothing
    [ROOT, ""],                                          # a hub with no answer
    [ROOT, "ACPI(_SB_)#ACPI(PCI0)"],                     # not a PCI-rooted path
    [ROOT, ROOT + "#USB(2)#USB(4)"],                     # a hub whose parent hub is unknown
    [ROOT + "#USB(2)"],                                  # no root hub at all
])
def test_odd_trees_are_not_trusted(paths):
    assert not u.locations_plausible(paths)


# ---------------------------------------------------------------- hub scanner: native first, PowerShell as fallback
def _scanner(monkeypatch, locator, ps_calls):
    monkeypatch.setattr(u, "_query_hub", lambda path: (4, {}))
    hubs = [("USB\\ROOT_HUB30\\A", "\\\\?\\a"), ("USB\\VID_1&PID_2\\B", "\\\\?\\b")]
    monkeypatch.setattr(u, "_enumerate_hub_interfaces", lambda: hubs)

    def ps(script, env=None, timeout=120):
        ps_calls.append(1)
        return json.dumps([{"Id": "USB\\ROOT_HUB30\\A", "Paths": [ROOT]},
                           {"Id": "USB\\VID_1&PID_2\\B", "Paths": [ROOT + "#USB(1)"]}])
    return u.PortScanner(ps, locator=locator)


GOOD = {"usb\\root_hub30\\a": ROOT, "usb\\vid_1&pid_2\\b": ROOT + "#USB(1)"}


def test_native_locations_skip_powershell(monkeypatch):
    calls = []
    sc = _scanner(monkeypatch, lambda ids: dict(GOOD), calls)
    hubs = sc.hubs()
    assert [h.location for h in hubs] == [ROOT, ROOT + "#USB(1)"] and calls == []


def test_powershell_is_the_fallback(monkeypatch):
    for bad in (lambda ids: {}, lambda ids: {"usb\\root_hub30\\a": ROOT},
                lambda ids: {"usb\\root_hub30\\a": ROOT, "usb\\vid_1&pid_2\\b": "ACPI(x)"}):
        calls = []
        hubs = _scanner(monkeypatch, bad, calls).hubs()
        assert calls == [1] and hubs[1].location == ROOT + "#USB(1)"


def test_locator_that_raises_falls_back(monkeypatch):
    def boom(ids):
        raise OSError("no cfgmgr")
    calls = []
    assert _scanner(monkeypatch, boom, calls).hubs()[0].location == ROOT and calls == [1]


def test_failed_hub_read_is_not_retried_every_scan(monkeypatch):
    calls = []
    sc = _scanner(monkeypatch, None, calls)
    sc._run_ps = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("timed out"))
    for _ in range(3):
        with pytest.raises(OSError):
            sc.hubs()
    assert calls == [1]                                   # tried once, then remembered
    sc._fail = (sc._fail[0], time.monotonic() - 1, sc._fail[2])    # the waiting time is over
    with pytest.raises(OSError):
        sc.hubs()
    assert calls == [1, 1]


# ---------------------------------------------------------------- drive listing: native location, with fallback
def _backend(monkeypatch):
    be = w.WindowsBackend.__new__(w.WindowsBackend)
    be._to_drive = lambda r: r
    return be


ROW = {"Number": 4, "PnpId": "USBSTOR\\DISK&VEN_X\\1&0", "Location": "", "VidPid": "", "T": "cim=10 partitions+volumes=20 pnp=0"}


def test_drive_location_comes_from_native_lookup(monkeypatch):
    envs = []
    monkeypatch.setattr(w, "run_ps", lambda s, e=None, t=120, label="": envs.append(e) or json.dumps([dict(ROW)]))
    monkeypatch.setattr(u, "native_disk_location", lambda pnp: (ROOT + "#USB(2)", "154B:007E"))
    rows = _backend(monkeypatch)._list_rows()
    assert rows[0]["Location"] == ROOT + "#USB(2)" and rows[0]["VidPid"] == "154B:007E"
    assert envs == [{"USBLOCKBOX_NOPNP": "1"}]


def test_nothing_found_natively_switches_to_powershell(monkeypatch):
    envs = []
    monkeypatch.setattr(w, "run_ps", lambda s, e=None, t=120, label="": envs.append(e) or json.dumps([dict(ROW)]))

    def fail(pnp):
        raise OSError("cfgmgr")
    monkeypatch.setattr(u, "native_disk_location", fail)
    be = _backend(monkeypatch)
    be._list_rows()
    assert envs == [{"USBLOCKBOX_NOPNP": "1"}, None] and be._native_pnp is False
    be._list_rows()                                       # stays on PowerShell: one call, no skip flag
    assert envs[2:] == [None]


def test_single_drive_check_still_filters_the_disk(monkeypatch):
    envs = []
    monkeypatch.setattr(w, "run_ps", lambda s, e=None, t=120, label="": envs.append((e, label)) or json.dumps([dict(ROW)]))
    monkeypatch.setattr(u, "native_disk_location", lambda pnp: (ROOT + "#USB(2)", ""))
    _backend(monkeypatch)._list_rows(4)
    assert envs == [({"USBLOCKBOX_DISK": "4", "USBLOCKBOX_NOPNP": "1"}, "check USB disk 4")]


def test_script_reports_what_it_needs():
    for token in ("USBLOCKBOX_NOPNP", "PnpId", "$m1", "Stopwatch"):
        assert token in w._LIST_SCRIPT


# ---------------------------------------------------------------- system disk answer is reused
def test_system_disks_are_cached(monkeypatch):
    n = []
    monkeypatch.setattr(w, "run_ps", lambda *a, **k: n.append(1) or "[0]")
    be = w.WindowsBackend.__new__(w.WindowsBackend)
    assert be.system_disk_numbers() == {0} and be.system_disk_numbers() == {0}
    assert n == [1]


# ---------------------------------------------------------------- --compare-native
def test_compare_native_reports_matches_and_differences(monkeypatch, capsys):
    hubs = [("USB\\ROOT_HUB30\\A", "\\\\?\\a")]
    monkeypatch.setattr(u, "_enumerate_hub_interfaces", lambda: hubs)
    monkeypatch.setattr(u, "native_hub_locations", lambda ids: {i.lower(): ROOT for i in ids})
    row = dict(ROW, Location=ROOT + "#USB(2)", VidPid="154B:007E")

    def ps(script, env=None, timeout=120, label=""):
        if "USBLOCKBOX_IDS" in script:
            return json.dumps([{"Id": "USB\\ROOT_HUB30\\A", "Paths": [ROOT]}])
        return json.dumps([row])
    monkeypatch.setattr(w, "run_ps", ps)
    monkeypatch.setattr(u, "PortScanner", lambda run_ps, locator=None: type("S", (), {
        "_locations": lambda self, ids: {i.lower(): ROOT for i in ids},
        "hubs": lambda self: [], "port_speed": lambda self, loc: 2})())
    row.update(Style="GPT", Parts=[{"Number": 1, "Type": "Basic", "Size": 5, "Active": False, "Hidden": False}],
               Vols=[{"Letter": "E", "FS": "NTFS", "Label": "x", "Size": 5, "Free": 1}])
    mine = ("GPT", [{"Number": 1, "Type": "Basic", "Size": 5, "Active": False, "Hidden": False}],
            [{"Letter": "E", "FS": "NTFS", "Label": "x", "Size": 5, "Free": 2}])
    monkeypatch.setattr(w.nativedisk, "native_parts_and_volumes", lambda n: mine)
    monkeypatch.setattr(u, "native_disk_location", lambda pnp: (ROOT + "#USB(2)", "154B:007E"))
    assert w.compare_native() == 0
    out = capsys.readouterr().out
    assert "All answers match." in out and "USB 2.0" in out and "partitions/volumes of disk 4" in out
    monkeypatch.setattr(w.nativedisk, "native_parts_and_volumes", lambda n: ("MBR",) + mine[1:])
    assert w.compare_native() == 2 and "DIFFERENT" in capsys.readouterr().out
    monkeypatch.setattr(w.nativedisk, "native_parts_and_volumes", lambda n: mine)
    monkeypatch.setattr(u, "native_disk_location", lambda pnp: (ROOT + "#USB(9)", "154B:007E"))
    assert w.compare_native() == 2
    assert "DIFFERENT" in capsys.readouterr().out
