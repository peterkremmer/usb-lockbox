import json
import os

from usblockbox import config
from usblockbox.config import Settings


def _load(tmp_path, data):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return Settings.load(p), p


def test_default_dirs_are_under_the_app_data_folder(tmp_path):
    s = Settings.load(tmp_path / "missing.json")
    data = config.data_dir()
    assert str(data) == os.path.join(os.environ["USBLOCKBOX_HOME"], "data")
    assert s.csv_dir == str(data / "records")
    assert s.pdf_dir == str(data / "records" / "pdf")
    assert s.resolved_csv_dir() == s.csv_dir


def test_default_folders_are_saved_blank_so_the_app_can_move(tmp_path):
    s = Settings.load(tmp_path / "missing.json")
    p = tmp_path / "settings.json"
    s.save(p)
    saved = json.loads(p.read_text())
    assert saved["csv_dir"] == "" and saved["pdf_dir"] == ""
    s.csv_dir = str(tmp_path / "elsewhere")
    s.save(p)
    assert json.loads(p.read_text())["csv_dir"] == str(tmp_path / "elsewhere")


def test_shipped_secret_defaults():
    s = Settings()
    assert s.include_secrets_csv is False and s.include_secrets_pdf is True
    assert s.dry_run is True                      # first launch never erases


def test_hostile_values_are_clamped(tmp_path):
    s, _ = _load(tmp_path, {
        "filesystem": "NTFS'; Remove-Item C:\\ -Recurse #", "partition_style": "GPT; calc",
        "encryption_method": "x", "password_mode": "../../etc", "on_policy_method_conflict": 5,
        "volume_label": "A'; calc; '", "simulator_ports": 9999, "overwrite_passes": -4, "zero_edge_mb": "lots",
        "dry_run": "no",
        "colors": {"DONE": "red;background:url(x)", "FAILED": "#112233", "BOGUS": "#000000"},
    })
    assert s.filesystem == "exFAT" and s.partition_style == "GPT"
    assert s.encryption_method == "XtsAes256" and s.password_mode == "fixed"
    assert s.on_policy_method_conflict == "block"
    assert s.volume_label == "A calc" and ";" not in s.volume_label and "'" not in s.volume_label
    assert s.simulator_ports == 16 and s.overwrite_passes == 0 and s.zero_edge_mb == 1
    assert s.dry_run is True                               # a non-bool never turns real mode on
    assert s.colors["DONE"] == config.DEFAULT_COLORS["DONE"] and s.colors["FAILED"] == "#112233"
    assert "BOGUS" not in s.colors


def test_inverted_size_limits_reset(tmp_path):
    s, _ = _load(tmp_path, {"min_size_gb": 600, "max_size_gb": 10})
    assert s.min_size_gb < s.max_size_gb


def test_corrupt_file_is_kept_not_overwritten(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    s = Settings.load(p)
    assert s.dry_run is True
    assert (tmp_path / "settings.json.bad").exists()


def test_secret_roundtrip_and_failure_is_empty():
    s = Settings()
    s.set_fixed_password("correct horse battery staple")
    assert s.get_fixed_password() == "correct horse battery staple"
    s.fixed_password_enc = "dpapi-u:AAAA"                  # cannot be decrypted here
    assert s.get_fixed_password() == ""


def test_old_pin_setting_is_ignored(tmp_path):
    s, p = _load(tmp_path, {"settings_pin_hash": "00:11"})
    s.save(p)
    assert "settings_pin_hash" not in json.loads(p.read_text())


def test_port_settings_are_sanitised(tmp_path):
    s, _ = _load(tmp_path, {"hidden_ports": ["USB#A", "", 5, "x" * 999], "port_names": {"USB#A": "<b>Front</b> & \"left\"", "b": 7, "": "x"}})
    assert s.hidden_ports == sorted(["usb#a", "x" * 300])
    assert s.port_names == {"usb#a": "bFrontb  left"[:24]} or "<" not in s.port_names["usb#a"]
    assert all(c not in v for v in s.port_names.values() for c in "<>&\"'")


def test_old_settings_file_never_starts_in_erase_mode(tmp_path):
    s, _ = _load(tmp_path, {"dry_run": False})                    # no schema_version: written by an older build
    assert s.dry_run is True
    s2, _ = _load(tmp_path, {"dry_run": False, "schema_version": 2})
    assert s2.dry_run is False


def test_method_labels():
    assert config.method_label("XtsAes256") == "XTS-AES-256" and config.method_label("Aes128") == "AES-CBC-128"
    assert config.method_label("") == "unknown"
