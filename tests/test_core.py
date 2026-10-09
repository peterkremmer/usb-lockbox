import csv
from pathlib import Path

import pytest

from usblockbox import policy, records
from usblockbox.models import Severity, Verdict
from usblockbox.passwords import generate_passphrase, validate_password
from usblockbox.pipeline import Processor
from usblockbox.safety import SafetyError, revalidate
from usblockbox.scan import scan_drive


def scan(backend, settings, scenario, port=1, pol=None, hist=None):
    d = backend.add_scenario(scenario, port)
    pw = settings.get_fixed_password()
    # mimic what a real backend does: verify the password on already-encrypted drives
    if d.bitlocker.present and backend.verify_password(d, pw):
        pass
    return scan_drive(backend.get_drive(d.disk_number), settings, backend.system_disk_numbers(),
                      pol, {f"sim-port-{i}" for i in range(1, 17)}, hist or [])


# ------------------------------------------------------------------ safety
def test_system_disk_is_rejected(backend, settings):
    r = scan(backend, settings, "system_disk")
    assert r.verdict == Verdict.REJECTED
    assert any(f.code == "SYSTEM_DISK" for f in r.findings)


@pytest.mark.parametrize("scenario,code", [
    ("hw_encrypted", "HW_ENCRYPTED"), ("write_protected", "WRITE_PROTECTED"), ("too_small", "TOO_SMALL")])
def test_rejections(backend, settings, scenario, code):
    r = scan(backend, settings, scenario)
    assert r.verdict == Verdict.REJECTED and any(f.code == code for f in r.findings)


def test_unbound_port_rejected(backend, settings):
    d = backend.add_scenario("blank", 1)
    r = scan_drive(d, settings, backend.system_disk_numbers(), None, {"sim-port-9"}, [])
    assert any(f.code == "UNBOUND_PORT" for f in r.findings)


def test_fixed_disk_rejected_unless_allowed(backend, settings):
    d = backend.add_scenario("blank", 1)
    d.is_removable = False
    r = scan_drive(d, settings, set(), None, None, [])
    assert r.verdict == Verdict.REJECTED
    settings.allow_fixed_disks = True
    assert scan_drive(d, settings, set(), None, None, []).verdict != Verdict.REJECTED


def test_drive_holding_records_folder_rejected(backend, settings):
    d = backend.add_scenario("blank", 1)
    from usblockbox.models import VolumeInfo
    d.volumes = [VolumeInfo(drive_letter="E")]
    settings.csv_dir = "E:\\records"
    r = scan_drive(d, settings, set(), None, None, [])
    assert any(f.code == "HOLDS_APP_DATA" for f in r.findings)


def test_revalidate_detects_swapped_device(backend, settings):
    d = backend.add_scenario("blank", 1)
    backend.remove(d.serial)
    with pytest.raises(SafetyError):
        revalidate(backend, d, settings)
    d2 = backend.add_scenario("blank", 1)          # new device lands on the same disk number
    with pytest.raises(SafetyError):
        revalidate(backend, d, settings)
    revalidate(backend, d2, settings)


def test_never_acts_on_system_disk_even_if_called_directly(backend, settings):
    d = backend.add_scenario("system_disk", 1)
    from usblockbox.models import ScanResult
    forced = ScanResult(drive=d, verdict=Verdict.NEEDS_WORK)   # pretend a bug let it through
    run = Processor(backend, settings).run(forced, None, lambda *_: None)
    assert not run.ok
    assert not any("clear_disk" in x for x in backend.log)


# ------------------------------------------------------------------ scan classification
def test_blank_drive_needs_work(backend, settings):
    r = scan(backend, settings, "blank")
    assert r.verdict == Verdict.NEEDS_WORK and not r.content_found


def test_used_drive_flags_content(backend, settings):
    r = scan(backend, settings, "used_files")
    assert r.content_found and any(f.code == "CONTENT" for f in r.findings)


def test_odd_boot_flags(backend, settings):
    r = scan(backend, settings, "odd_boot")
    codes = {f.code for f in r.findings}
    assert {"BOOT_CODE", "MULTI_PART", "ODD_PART", "ACTIVE_FLAG"} <= codes


def test_wrong_method_detected(backend, settings):
    r = scan(backend, settings, "wrong_method")
    assert any(f.code == "ENC_WRONG" for f in r.findings)


def test_locked_unknown_is_wiped(backend, settings):
    r = scan(backend, settings, "locked_unknown")
    assert r.verdict == Verdict.NEEDS_WORK and any(f.code == "ENC_LOCKED" for f in r.findings)


def test_compliant_empty_is_refreshed_by_default(backend, settings):
    d = backend.add_scenario("compliant_empty", 1)
    d.bitlocker.unlocked_with_fixed_password = True
    r = scan_drive(d, settings, set(), None, None, [{"outcome": "PROCESSED"}])
    assert r.verdict == Verdict.NEEDS_WORK and any(f.code == "REFRESH" for f in r.findings)


def test_compliant_empty_skipped_only_with_opt_in_and_history(backend, settings):
    d = backend.add_scenario("compliant_empty", 1)
    d.bitlocker.unlocked_with_fixed_password = True
    settings.reuse_compliant_drives = True
    assert scan_drive(d, settings, set(), None, None, []).verdict == Verdict.NEEDS_WORK     # no history
    assert scan_drive(d, settings, set(), None, None, [{"outcome": "PROCESSED"}]).verdict == Verdict.ALREADY_OK


# ------------------------------------------------------------------ policy
def test_policy_method_conflict_blocks(settings):
    rep = policy.evaluate({"GPO": {policy.GPO_REMOVABLE_METHOD: 6}, "Intune/MDM": {}}, settings)
    assert rep.blocks and "XTS-AES-128" in rep.blocks[0].message


def test_policy_method_conflict_follow_policy(settings):
    settings.on_policy_method_conflict = "use_policy"
    rep = policy.evaluate({"GPO": {policy.GPO_REMOVABLE_METHOD: 6}, "Intune/MDM": {}}, settings)
    assert not rep.blocks and rep.effective_method == "XtsAes128"


def test_policy_disabled_blocks_scan(backend, settings):
    rep = policy.evaluate({"GPO": {policy.GPO_REMOVABLE_CONFIGURE: 0}, "Intune/MDM": {}}, settings)
    d = backend.add_scenario("blank", 1)
    r = scan_drive(d, settings, set(), rep, None, [])
    assert r.verdict == Verdict.REJECTED and "policy" in r.reasons("BLOCK")[0].lower()


def test_policy_password_rules(settings):
    rep = policy.evaluate({"GPO": {policy.GPO_PASS_LENGTH: 20, policy.GPO_PASS_COMPLEXITY: 1}, "Intune/MDM": {}},
                          settings, password="shortpw")
    assert len(rep.blocks) == 2


def test_explain_error_mentions_policy(settings):
    rep = policy.evaluate({"GPO": {policy.GPO_REMOVABLE_CONFIGURE: 0}, "Intune/MDM": {}}, settings)
    msg = policy.explain_error("Access is denied. (0x80070005)", rep)
    assert "Access denied" in msg and "disables BitLocker" in msg


# ------------------------------------------------------------------ passwords
def test_generated_passphrase_properties():
    pw = generate_passphrase(24)
    assert len(pw) == 24 and generate_passphrase(24) != pw


def test_password_validation(settings):
    assert validate_password("x" * 5, settings) is not None
    assert validate_password("x" * 20, settings) is None


# ------------------------------------------------------------------ pipeline end to end
def test_full_run_and_records(backend, settings):
    scn = scan(backend, settings, "used_files")
    steps = []
    run = Processor(backend, settings).run(scn, None, lambda l, f: steps.append((l, f)))
    assert run.ok and run.outcome == "PROCESSED"
    assert run.recovery_key.count("-") == 7 and run.password == settings.get_fixed_password()
    assert run.sanitization_category.startswith("Clear (overwrite x3)")
    assert run.destroy_note        # unknown history -> honesty note
    assert steps[-1][1] == pytest.approx(1.0)
    cpath, ppath, warns = records.record_run(settings, scn, run, backend.name)
    assert not warns and ppath.exists() and ppath.stat().st_size > 1000
    rows = list(csv.DictReader(cpath.open(encoding="utf-8-sig")))
    assert len(rows) == 1 and rows[0]["recovery_key"] == run.recovery_key and rows[0]["password"] == run.password
    assert records.verify_chain(settings) == (True, 0)


def test_secrets_toggle_off(backend, settings):
    settings.include_secrets_csv = False
    settings.include_secrets_pdf = False
    scn = scan(backend, settings, "blank")
    run = Processor(backend, settings).run(scn, None, lambda *_: None)
    cpath, ppath, _ = records.record_run(settings, scn, run, backend.name)
    row = list(csv.DictReader(cpath.open(encoding="utf-8-sig")))[0]
    assert run.recovery_key not in open(cpath, encoding="utf-8-sig").read()
    assert "not recorded" in row["recovery_key"]


def test_hash_chain_detects_tampering(backend, settings):
    for port in (1, 2, 3):
        scn = scan(backend, settings, "blank", port)
        run = Processor(backend, settings).run(scn, None, lambda *_: None)
        records.record_run(settings, scn, run, backend.name)
    assert records.verify_chain(settings) == (True, 0)
    p = records.csv_path(settings)
    text = p.read_text(encoding="utf-8-sig").replace("PROCESSED", "ALREADY_COMPLIANT", 1)
    p.write_text(text, encoding="utf-8-sig")
    ok, bad = records.verify_chain(settings)
    assert not ok and bad == 1


def test_history_lookup_and_formula_escape(backend, settings):
    scn = scan(backend, settings, "blank")
    run = Processor(backend, settings).run(scn, None, lambda *_: None)
    run.password = "=cmd|' /C calc'!A0"
    records.record_run(settings, scn, run, backend.name)
    assert records.read_history(settings, scn.drive.serial)[0]["outcome"] == "PROCESSED"
    assert records.verify_chain(settings)[0]
    assert "'=cmd" in records.csv_path(settings).read_text(encoding="utf-8-sig")


def test_failure_injection_marks_failed_and_explains(backend, settings):
    scn = scan(backend, settings, "blank")
    backend.fail_at(scn.drive.serial, "enable_bitlocker")
    run = Processor(backend, settings).run(scn, None, lambda *_: None)
    assert not run.ok and run.outcome == "FAILED" and "Access denied" in run.possible_cause
    assert [s.ok for s in run.steps][-1] is False


def test_surprise_removal_fails(backend, settings):
    scn = scan(backend, settings, "blank")
    import threading, time
    threading.Timer(0.1, lambda: backend.remove(scn.drive.serial)).start()
    settings.overwrite_passes = 7
    slow = type(backend)(speed=1.0)
    slow._disks = backend._disks
    run = Processor(slow, settings).run(scn, None, lambda *_: None)
    assert not run.ok


def test_emergency_stop(backend, settings):
    scn = scan(backend, settings, "blank")
    slow = type(backend)(speed=1.0); slow._disks = backend._disks
    proc = Processor(slow, settings)
    import threading
    threading.Timer(0.3, proc.cancel).start()
    run = proc.run(scn, None, lambda *_: None)
    assert not run.ok and "Cancelled" in run.error


def test_dry_run_writes_nothing(backend, settings):
    settings.dry_run = True
    scn = scan(backend, settings, "used_files")
    run = Processor(backend, settings).run(scn, None, lambda *_: None)
    assert run.ok and run.outcome == "DRY_RUN" and backend.log == []


def test_policy_conflict_use_policy_method_applied(backend, settings):
    settings.on_policy_method_conflict = "use_policy"
    rep = policy.evaluate({"GPO": {policy.GPO_REMOVABLE_METHOD: 6}, "Intune/MDM": {}}, settings)
    scn = scan(backend, settings, "blank", pol=rep)
    run = Processor(backend, settings).run(scn, rep, lambda *_: None)
    assert run.ok and run.encryption_method == "XtsAes128"


def test_missing_fixed_password_fails_before_any_write(backend, settings):
    settings.fixed_password_enc = ""
    scn = scan(backend, settings, "used_files")
    run = Processor(backend, settings).run(scn, None, lambda *_: None)
    assert not run.ok and backend.log == []


def _win_backend(monkeypatch, ps_log, dp_log, ps_reply="\r\nE\r\n"):
    import contextlib
    from usblockbox.backends import windows as w
    be = w.WindowsBackend.__new__(w.WindowsBackend)
    monkeypatch.setattr(w.WindowsBackend, "_diskpart", lambda self, s, timeout=120: dp_log.append(s) or "")
    monkeypatch.setattr(w.WindowsBackend, "_shell_hw_paused", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(w, "run_ps", lambda script, env=None, timeout=120: ps_log.append(script) or ps_reply)
    return w, be


def test_windows_partition_step_formats_before_it_assigns_a_letter(monkeypatch):
    from usblockbox.backends.base import BackendError
    from usblockbox.models import DriveInfo
    ps, dp = [], []
    w, be = _win_backend(monkeypatch, ps, dp)
    d = DriveInfo(2, "uid", "ser")
    assert be.init_partition_format(d, "gpt", "exFAT", "Secure USB!") == "E"
    assert "select disk 2" in dp[0] and "clean" in dp[0] and "convert gpt" in dp[0]
    script = ps[0]
    assert "ErrorActionPreference='Stop'" in script and "'Secure USB'" in script       # label sanitised
    assert "New-Partition -DiskNumber 2 -UseMaximumSize;" in script and "-AssignDriveLetter;" in script
    assert script.index("Format-Volume -Partition") < script.index("Add-PartitionAccessPath")   # letter comes last
    assert "New-Partition -DiskNumber 2 -UseMaximumSize -AssignDriveLetter" not in script
    with pytest.raises(BackendError):
        be.init_partition_format(d, "weird", "exFAT", "X")


def test_windows_partition_step_reports_diskpart_failure(monkeypatch):
    from usblockbox.backends.base import BackendError
    from usblockbox.models import DriveInfo
    ps, dp = [], []
    w, be = _win_backend(monkeypatch, ps, dp)

    def fail(self, s, timeout=120):
        raise BackendError("diskpart failed: not convertible", "DISKPART")
    monkeypatch.setattr(w.WindowsBackend, "_diskpart", fail)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    with pytest.raises(BackendError) as e:
        be.init_partition_format(DriveInfo(2, "uid", "ser"), "GPT", "exFAT", "X")
    # one refresh before the single retry, but never a partition or format command
    assert "not convertible" in str(e.value) and not any("New-Partition" in x or "Format-Volume" in x for x in ps)


def test_shell_hardware_detection_is_paused_once_and_always_restarted(monkeypatch):
    from usblockbox.backends import windows as w
    calls = []

    def fake_ps(script, env=None, timeout=120):
        calls.append("stop" if "Stop-Service" in script else "start" if "Start-Service" in script else "other")
        return "stopped" if "Stop-Service" in script else ""
    monkeypatch.setattr(w, "run_ps", fake_ps)
    monkeypatch.setattr(w.atexit, "register", lambda *a, **k: None)
    w.WindowsBackend._hw_users, w.WindowsBackend._hw_stopped = 0, False
    a = w.WindowsBackend.__new__(w.WindowsBackend)
    with a._shell_hw_paused():
        with a._shell_hw_paused():                       # a second drive processing at the same time
            pass
        assert calls == ["stop"]                         # not paused twice, not restarted while one is still busy
    assert calls == ["stop", "start"]
    with pytest.raises(RuntimeError):                    # a failure inside still restarts it
        with a._shell_hw_paused():
            raise RuntimeError("boom")
    assert calls == ["stop", "start", "stop", "start"]
    # service not running to begin with: it is left alone
    calls.clear()
    monkeypatch.setattr(w, "run_ps", lambda s, env=None, timeout=120: calls.append(s) or "skip")
    with a._shell_hw_paused():
        pass
    assert len(calls) == 1 and "Start-Service" not in calls[0]
