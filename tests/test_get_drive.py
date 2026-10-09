"""The safety re-check before every destructive step must read ONE drive, one at a time."""
import json
import threading
import time
from types import SimpleNamespace

from usblockbox.backends import windows as w
from usblockbox import policy


def _backend():
    be = w.WindowsBackend.__new__(w.WindowsBackend)
    be._to_drive = lambda r: SimpleNamespace(disk_number=int(r["Number"]))
    return be


def test_get_drive_asks_for_only_that_disk(monkeypatch):
    seen = []

    def fake_ps(script, env_extra=None, timeout=120, label=""):
        seen.append((dict(env_extra or {}), label))
        return json.dumps([{"Number": 7}])

    monkeypatch.setattr(w, "run_ps", fake_ps)
    monkeypatch.setattr(w.usbports, "native_disk_location", lambda pnp: ("PCIROOT(0)#USBROOT(0)#USB(1)", ""))
    d = _backend().get_drive(7)
    assert d.disk_number == 7
    assert seen == [({"USBLOCKBOX_DISK": "7", "USBLOCKBOX_NOPNP": "1"}, "check USB disk 7")]
    assert "USBLOCKBOX_DISK" in w._LIST_SCRIPT and "-Filter" in w._LIST_SCRIPT


def test_get_drive_returns_none_when_disk_is_gone(monkeypatch):
    monkeypatch.setattr(w, "run_ps", lambda *a, **k: "[]")
    assert _backend().get_drive(3) is None


def test_list_usb_disks_still_lists_everything(monkeypatch):
    be, _calls = _fake_world(monkeypatch, [1, 2])
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 2]


def test_rechecks_do_not_run_in_parallel(monkeypatch):
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_ps(script, env_extra=None, timeout=120, label=""):
        with lock:
            active[0] += 1; peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return json.dumps([{"Number": int(env_extra["USBLOCKBOX_DISK"])}])

    monkeypatch.setattr(w, "run_ps", slow_ps)
    monkeypatch.setattr(w.usbports, "native_disk_location", lambda pnp: ("PCIROOT(0)#USBROOT(0)#USB(1)", ""))
    be = _backend()
    ts = [threading.Thread(target=be.get_drive, args=(n,)) for n in (1, 2, 3, 4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 1


def test_timeout_has_a_plain_explanation():
    msg = "PowerShell failed to run: Command '[...]' timed out after 90 seconds"
    assert "took too long" in policy.explain_error(msg, None)


# ---------------------------------------------------------------- the drive listing: who is there, then details once per drive
def _fake_world(monkeypatch, numbers, fail=()):
    """A backend whose PowerShell answers from a list of plugged-in disk numbers. Returns (backend, call log)."""
    calls = []
    present = list(numbers)

    def fake_ps(script, env_extra=None, timeout=120, label=""):
        calls.append(label)
        if script is w._QUICK_SCRIPT:
            return json.dumps([{"Number": n, "UniqueId": "u%d" % n, "Serial": "s%d" % n, "Size": 1000 + n}
                               for n in present])
        n = int(env_extra["USBLOCKBOX_DISK"])
        if n in fail:
            raise w.BackendError("PowerShell failed to run: timed out", "PS_LAUNCH")
        return json.dumps([{"Number": n, "PnpId": "x%d" % n}])

    monkeypatch.setattr(w, "run_ps", fake_ps)
    monkeypatch.setattr(w.usbports, "native_disk_location", lambda pnp: ("PCIROOT(0)#USBROOT(0)#USB(1)", ""))
    be = w.WindowsBackend.__new__(w.WindowsBackend)
    be._to_drive = lambda r: SimpleNamespace(disk_number=int(r["Number"]))
    be.present = present
    w.WindowsBackend._detail_lock = threading.Lock()
    return be, calls


def test_details_are_read_once_per_drive(monkeypatch):
    be, calls = _fake_world(monkeypatch, [1, 2, 3])
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 2, 3]
    assert sorted(c for c in calls if c != "list USB disk identities") == ["check USB disk 1", "check USB disk 2", "check USB disk 3"]
    calls.clear()
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 2, 3]
    assert calls == ["list USB disk identities"]                  # second poll: the quick call only


def test_a_new_drive_is_read_and_a_removed_one_forgotten(monkeypatch):
    be, calls = _fake_world(monkeypatch, [1, 2])
    be.list_usb_disks(); calls.clear()
    be.present.append(3)
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 2, 3]
    assert calls == ["list USB disk identities", "check USB disk 3"]
    be.present.remove(2); calls.clear()
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 3]
    be.present.append(2); calls.clear()
    be.list_usb_disks()
    assert "check USB disk 2" in calls                            # plugged in again: read again


def test_a_different_drive_in_the_same_slot_is_read_again(monkeypatch):
    be, calls = _fake_world(monkeypatch, [1])
    be.list_usb_disks(); calls.clear()
    be._detail[1] = ((1, "other", "s1", 1001), be._detail[1][1], be._detail[1][2])   # what was cached is another drive
    be.list_usb_disks()
    assert "check USB disk 1" in calls


def test_one_drive_that_will_not_answer_does_not_hide_the_others(monkeypatch):
    be, calls = _fake_world(monkeypatch, [1, 2, 3], fail={2})
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 3]
    calls.clear()
    assert [d.disk_number for d in be.list_usb_disks()] == [1, 3]
    assert "check USB disk 2" not in calls                        # not retried straight away
    monkeypatch.setattr(w, "DETAIL_RETRY", 0)
    be.list_usb_disks()
    assert "check USB disk 2" in calls


def test_drives_are_reported_as_they_become_ready(monkeypatch):
    be, _calls = _fake_world(monkeypatch, [1, 2, 3])
    got = []
    be.list_usb_disks(on_ready=lambda ds: got.append([d.disk_number for d in ds]))
    assert got[-1] == [1, 2, 3] and len(got) == 3 and all(len(a) < len(b) for a, b in zip(got, got[1:]))
    got.clear()
    be.list_usb_disks(on_ready=lambda ds: got.append([d.disk_number for d in ds]))
    assert got == [[1, 2, 3]]                                     # all remembered: reported at once


def test_invalidate_cache_reads_everything_again(monkeypatch):
    be, calls = _fake_world(monkeypatch, [1, 2])
    be.list_usb_disks(); be.invalidate_cache(); calls.clear()
    be.list_usb_disks()
    assert sorted(calls) == ["check USB disk 1", "check USB disk 2", "list USB disk identities"]


# ---------------------------------------------------------------- a drive that will not answer is reported, with its place
def test_disk_path_becomes_a_device_instance_id():
    p = r"\\?\usbstor#disk&ven_kingston&prod_dt&rev_1.00#0123456789&0#{53f56307-b6bf-11d0-94f2-00a0c91efb8b}"
    assert w.pnp_id_from_disk_path(p) == "USBSTOR\\DISK&VEN_KINGSTON&PROD_DT&REV_1.00\\0123456789&0"
    assert w.pnp_id_from_disk_path("") == "" and w.pnp_id_from_disk_path("garbage") == ""


def test_unreadable_drive_is_reported_with_its_location(monkeypatch):
    be, _calls = _fake_world(monkeypatch, [1, 2], fail={2})
    real_ps = w.run_ps

    def with_paths(script, env_extra=None, timeout=120, label=""):
        out = real_ps(script, env_extra, timeout, label)
        if script is w._QUICK_SCRIPT:
            rows = json.loads(out)
            for r in rows:
                r["Path"] = r"\\?\usbstor#disk&ven_a#%d&0#{53f56307-b6bf-11d0-94f2-00a0c91efb8b}" % r["Number"]
            return json.dumps(rows)
        return out

    monkeypatch.setattr(w, "run_ps", with_paths)
    monkeypatch.setattr(w.usbports, "native_disk_location",
                        lambda pnp: ("PCIROOT(0)#USBROOT(0)#USB(%s)" % pnp.rsplit("\\", 1)[1][0], ""))
    assert [d.disk_number for d in be.list_usb_disks()] == [1]
    assert be.unreadable_drives() == [("PCIROOT(0)#USBROOT(0)#USB(2)", "Windows is not answering for this drive")]
    monkeypatch.setattr(w, "DETAIL_RETRY", 0)
    be.present.remove(2); be.list_usb_disks()
    assert be.unreadable_drives() == []                          # unplugged: no longer reported


# ---------------------------------------------------------------- the listing must leave a disk alone while it is being processed
def test_a_disk_being_processed_is_not_read_again(monkeypatch):
    be, calls = _fake_world(monkeypatch, [1, 2])
    be.list_usb_disks(); calls.clear()
    be.hold(2)
    be._detail[2] = ((2, "other", "s2", 0), be._detail[2][1], 0.0)     # stale and "different": would normally be re-read
    got = be.list_usb_disks()
    assert [d.disk_number for d in got] == [1, 2]                          # still listed, from what was already known
    assert calls == ["list USB disk identities"]                           # but not read
    be.release(2)
    be.list_usb_disks()
    assert "check USB disk 2" in calls                                     # normal again once processing ends


def test_pipeline_holds_the_disk_while_it_runs(monkeypatch):
    from usblockbox.backends.simulated import SimulatedBackend
    from usblockbox.config import Settings
    from usblockbox.pipeline import Processor
    from usblockbox.scan import scan_drive
    be = SimulatedBackend(speed=50.0)
    be.add_scenario("blank", 1)
    held = []
    orig_hold, orig_release = be.hold, be.release
    be.hold = lambda n: (held.append(("hold", n)), orig_hold(n))[1]
    be.release = lambda n: (held.append(("release", n)), orig_release(n))[1]
    s = Settings(); s.dry_run = False; s.overwrite_passes = 0; s.password_mode = "prompt"
    drive = be.list_usb_disks()[0]
    run = Processor(be, s).run(scan_drive(drive, s, be.system_disk_numbers()), None, lambda l, f: None, lambda: "Passw0rd!Passw0rd")
    assert held and held[0][0] == "hold" and held[-1][0] == "release"


def _bl(**kw):
    base = {"Present": True, "Protected": True, "Method": "XtsAes256", "Percent": 100.0, "Locked": False,
            "Protectors": ["Password"], "Status": "FullyEncrypted"}
    base.update(kw)
    return json.dumps(base)


def _read(monkeypatch, info_json, busy=()):
    be, _calls = _fake_world(monkeypatch, [1])
    locks = []
    be.verify_password = lambda d, pw: locks.append(1) or True
    be._pw = lambda: "pw"
    be.__dict__["_busy"] = set(busy)
    monkeypatch.setattr(w, "run_ps", lambda *a, **k: info_json)
    monkeypatch.setattr(w.WindowsBackend, "_count_files", staticmethod(lambda letter: (0, "")))
    d = w.DriveInfo(1, "u", "s", volumes=[w.VolumeInfo(drive_letter="E")])
    be._fill_bitlocker_and_files(d)
    return be, d, locks


def test_reading_a_drive_never_locks_or_unlocks_it(monkeypatch):
    for info in (_bl(), _bl(Percent=42.0, Status="EncryptionInProgress"), _bl(Locked=True)):
        _be, d, locks = _read(monkeypatch, info)
        assert locks == [] and d.bitlocker.present                         # status only, whatever state it is in


def test_password_is_tested_only_on_a_fully_encrypted_drive_that_is_not_being_processed(monkeypatch):
    be, d, locks = _read(monkeypatch, _bl(Percent=42.0, Status="EncryptionInProgress"))
    be.prove_fixed_password(d, "pw")
    assert locks == [] and not d.bitlocker.unlocked_with_fixed_password     # still encrypting: left strictly alone
    be, d, locks = _read(monkeypatch, _bl(Status="EncryptionSuspended"))
    be.prove_fixed_password(d, "pw")
    assert locks == []                                                       # paused is not finished either
    be, d, locks = _read(monkeypatch, _bl(), busy=[1])
    be.prove_fixed_password(d, "pw")
    assert locks == []                                                       # this app is working on it right now
    be, d, locks = _read(monkeypatch, _bl())
    be.prove_fixed_password(d, "")
    assert locks == []                                                       # no fixed password to test with
    be.prove_fixed_password(d, "pw")
    assert locks == [1] and d.bitlocker.unlocked_with_fixed_password


def test_a_locked_drive_the_password_opens_gets_its_files_counted(monkeypatch):
    be, d, locks = _read(monkeypatch, _bl(Locked=True))
    assert d.volumes[0].file_count is None                                   # could not be counted while locked
    be.prove_fixed_password(d, "pw")
    assert d.bitlocker.unlocked_with_fixed_password and not d.bitlocker.locked and d.volumes[0].file_count == 0
