import hashlib
import io
import json
import zipfile

import pytest

from usblockbox import updater
from usblockbox.updater import UpdateError

REPO = "someone/usblockbox"


def make_zip(files, wrapper="usblockbox-1.2.0/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(wrapper + name, data)
    return buf.getvalue()


GOOD_FILES = {"run_usblockbox.py": "print('new')", "usblockbox/__init__.py": "__version__='1.2.0'", "usblockbox/__main__.py": "y=2",
              "usblockbox/core.py": "x=1", "data/settings.json": "{\"hostile\": true}"}


def release(zip_bytes=None, name="usblockbox-1.2.0.zip", tag="v1.2.0", **kw):
    rel = {"tag_name": tag, "html_url": f"https://github.com/{REPO}/releases/tag/{tag}", "body": "notes",
           "draft": False, "prerelease": False, "assets": []}
    if zip_bytes is not None:
        rel["assets"].append({"name": name, "browser_download_url": f"https://github.com/{REPO}/releases/download/{tag}/{name}"})
        rel["assets"].append({"name": "SHA256SUMS", "browser_download_url": f"https://github.com/{REPO}/releases/download/{tag}/SHA256SUMS"})
    rel.update(kw)
    return rel


def fetcher(rel, zip_bytes=None, sums=None):
    def fetch(url, max_bytes):
        if url.endswith("/releases/latest"):
            return json.dumps(rel).encode()
        if url.endswith("SHA256SUMS"):
            return (sums if sums is not None else f"{hashlib.sha256(zip_bytes).hexdigest()}  usblockbox-1.2.0.zip\n").encode()
        if url.endswith(".zip"):
            return zip_bytes
        raise AssertionError(url)
    return fetch


# ---------------------------------------------------------------- versions
@pytest.mark.parametrize("remote,local,expected", [
    ("v1.2.0", "1.1.9", True), ("1.10.0", "1.9.0", True), ("1.0.0", "1.0.0", False), ("1.0", "1.0.0", False),
    ("v0.9.0", "0.10.0", False), ("1.0.0", "1.0.0-rc1", True), ("1.0.0-rc2", "1.0.0", False),
])
def test_is_newer(remote, local, expected):
    assert updater.is_newer(remote, local) is expected


def test_bad_version_rejected():
    with pytest.raises(UpdateError):
        updater.parse_version("latest!")


def test_disabled_until_repo_configured():
    assert not updater.enabled("OWNER/REPO") and not updater.enabled("not a repo") and updater.enabled(REPO)
    with pytest.raises(UpdateError):
        updater.check_for_update("1.0.0", "OWNER/REPO", fetch=lambda *a: b"")


# ---------------------------------------------------------------- check
def test_newer_release_found_with_checksum():
    z = make_zip(GOOD_FILES)
    info = updater.check_for_update("1.0.0", REPO, fetcher(release(z), z))
    assert info and info.version == "1.2.0" and info.can_self_update
    assert info.sha256 == hashlib.sha256(z).hexdigest()


def test_up_to_date_draft_and_prerelease_ignored():
    z = make_zip(GOOD_FILES)
    assert updater.check_for_update("1.2.0", REPO, fetcher(release(z), z)) is None
    assert updater.check_for_update("1.0.0", REPO, fetcher(release(z, draft=True), z)) is None
    assert updater.check_for_update("1.0.0", REPO, fetcher(release(z, prerelease=True), z)) is None


def test_release_without_checksum_is_manual_only():
    z = make_zip(GOOD_FILES)
    rel = release(z)
    rel["assets"] = rel["assets"][:1]                       # zip but no SHA256SUMS
    info = updater.check_for_update("1.0.0", REPO, fetcher(rel, z))
    assert info and not info.can_self_update
    with pytest.raises(UpdateError):
        updater.download_zip(info, fetcher(rel, z))


def test_checksum_from_release_notes():
    z = make_zip(GOOD_FILES)
    rel = release(z, body=f"usblockbox-1.2.0.zip sha256: {hashlib.sha256(z).hexdigest()}")
    rel["assets"] = rel["assets"][:1]
    assert updater.check_for_update("1.0.0", REPO, fetcher(rel, z)).can_self_update


def test_malformed_api_response():
    with pytest.raises(UpdateError):
        updater.check_for_update("1.0.0", REPO, lambda u, m: b"<html>")


def test_hosts_are_restricted():
    for bad in ("http://api.github.com/x", "https://evil.example/x", "https://github.com.evil.example/x", "file:///etc/passwd"):
        with pytest.raises(UpdateError):
            updater._check_url(bad)
    updater._check_url("https://objects.githubusercontent.com/x")


def test_checksum_mismatch_blocks_install():
    z = make_zip(GOOD_FILES)
    info = updater.check_for_update("1.0.0", REPO, fetcher(release(z), z))
    with pytest.raises(UpdateError, match="Checksum mismatch"):
        updater.download_zip(info, fetcher(release(z), b"tampered"))


# ---------------------------------------------------------------- apply
def test_apply_update_replaces_code_keeps_data_and_backs_up(tmp_path):
    (tmp_path / "usblockbox").mkdir()
    (tmp_path / "usblockbox" / "core.py").write_text("old")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text("mine")
    written = updater.apply_update(make_zip(GOOD_FILES), tmp_path, "1.0.0")
    assert (tmp_path / "usblockbox" / "core.py").read_text() == "x=1"
    assert (tmp_path / "run_usblockbox.py").exists()
    assert (tmp_path / "data" / "settings.json").read_text() == "mine"          # data/ is never overwritten
    assert "data/settings.json" not in written
    backups = list((tmp_path / ".update_backup").rglob("core.py"))
    assert backups and backups[0].read_text() == "old"
    assert not list(tmp_path.rglob("*.update_tmp"))


@pytest.mark.parametrize("evil", ["../evil.py", "/abs/evil.py", "C:/evil.py", "usblockbox/../../evil.py"])
def test_zip_slip_rejected(tmp_path, evil):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("run_usblockbox.py", "x"); z.writestr("usblockbox/__init__.py", "x"); z.writestr(evil, "boom")
    with pytest.raises(UpdateError):
        updater.apply_update(buf.getvalue(), tmp_path)
    assert not (tmp_path.parent / "evil.py").exists()


def test_symlink_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("run_usblockbox.py", "x"); z.writestr("usblockbox/__init__.py", "x")
        info = zipfile.ZipInfo("link"); info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    with pytest.raises(UpdateError, match="Symbolic link"):
        updater.apply_update(buf.getvalue(), tmp_path)


def test_unrelated_zip_rejected(tmp_path):
    with pytest.raises(UpdateError, match="does not look like"):
        updater.apply_update(make_zip({"readme.txt": "hi"}), tmp_path)
    with pytest.raises(UpdateError):
        updater.apply_update(b"not a zip", tmp_path)


def test_git_checkout_is_never_modified(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(UpdateError, match="git"):
        updater.apply_update(make_zip(GOOD_FILES), tmp_path)
    assert not (tmp_path / "run_usblockbox.py").exists()


def test_no_release_yet_is_explained_not_called_unreachable():
    def fetch(url, max_bytes):
        raise UpdateError("GitHub answered HTTP 404 (Not Found).", status=404)
    with pytest.raises(UpdateError) as e:
        updater.check_for_update("0.1.0", "owner/repo", fetch)
    assert "No release has been published yet" in str(e.value) and "Could not reach" not in str(e.value)


def test_other_http_errors_are_passed_through():
    def fetch(url, max_bytes):
        raise UpdateError("GitHub answered HTTP 403 (rate limit exceeded).", status=403)
    with pytest.raises(UpdateError) as e:
        updater.check_for_update("0.1.0", "owner/repo", fetch)
    assert "403" in str(e.value)


def test_archive_without_the_app_package_is_refused(tmp_path):
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("usblockbox-9.9.9/readme.txt", "not the app")
    with pytest.raises(UpdateError):
        updater.apply_update(z.getvalue(), tmp_path, "0.1.0")
