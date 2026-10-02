"""Update check and optional self-update from GitHub Releases. No Qt in here, so it is easy to test.

Design rules (this code downloads and installs code, so it is deliberately strict):
 * Only HTTPS, and only to a short list of GitHub hosts (also enforced on every redirect).
 * Self-update needs a release *asset* zip AND a published SHA-256 for it. Source archives
   (zipball) cannot be checksummed ahead of time, so for those the app only points at the release page.
 * The zip is validated before anything is written: no absolute paths, no "..", no symlinks, size caps,
   and it must look like this app. User data (data/) is never touched.
 * Replaced files are backed up first. Nothing runs automatically: the user always confirms, and the
   app asks to be restarted afterwards.
 * A git checkout is never modified; the user is told to `git pull` instead.
 * The only information sent is a normal HTTPS request with a User-Agent of "<app>/<version>".
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Optional
from urllib.parse import urlparse

from . import __version__
from .appinfo import APP_SLUG, UPDATE_REPO

ALLOWED_HOSTS = {
    "api.github.com", "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "codeload.github.com",
}
PLACEHOLDER_REPO = "OWNER/REPO"
MAX_API_BYTES = 1_000_000
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 150 * 1024 * 1024
MAX_FILES = 3000
PROTECTED_TOP_LEVEL = {"data", ".git", ".update_backup"}     # never overwritten by an update
Fetch = Callable[[str, int], bytes]                           # (url, max_bytes) -> body


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    notes: str
    page_url: str
    zip_url: str = ""          # release asset zip ("" = none: manual install only)
    zip_name: str = ""
    sha256: str = ""           # "" = no published checksum: manual install only

    @property
    def can_self_update(self) -> bool:
        return bool(self.zip_url and self.sha256)


# ---------------------------------------------------------------- versions
def parse_version(text: str) -> tuple[tuple[int, ...], bool]:
    """'v1.2.3-rc1' -> ((1, 2, 3), True). The flag marks a pre-release."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})(?:[-+.]?([0-9A-Za-z.-]+))?\s*$", text or "")
    if not m:
        raise UpdateError(f"Unrecognised version: {text!r}")
    return tuple(int(x) for x in m.group(1).split(".")), bool(m.group(2))


def is_newer(remote: str, local: str = __version__) -> bool:
    (rn, rpre), (ln, lpre) = parse_version(remote), parse_version(local)
    width = max(len(rn), len(ln))
    rn, ln = rn + (0,) * (width - len(rn)), ln + (0,) * (width - len(ln))
    if rn != ln:
        return rn > ln
    return lpre and not rpre            # 1.0.0 is newer than 1.0.0-rc1


# ---------------------------------------------------------------- network
def enabled(repo: str = UPDATE_REPO) -> bool:
    return repo != PLACEHOLDER_REPO and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo))


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "").lower() not in ALLOWED_HOSTS:
        raise UpdateError(f"Refusing to contact {u.hostname or url!r}: only HTTPS GitHub hosts are allowed.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, max_bytes: int, timeout: float = 15.0) -> bytes:
    _check_url(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_SLUG}/{__version__}",
                                               "Accept": "application/vnd.github+json, application/octet-stream"})
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"Could not reach GitHub: {e}")
    if len(body) > max_bytes:
        raise UpdateError("Download is larger than the allowed size.")
    return body


# ---------------------------------------------------------------- check
def _find_sha256(release: dict, zip_name: str, assets: list[dict], fetch: Fetch) -> str:
    """SHA-256 of the zip from a checksum asset (SHA256SUMS / <zip>.sha256) or the release notes."""
    for a in assets:
        n = str(a.get("name", ""))
        if n.lower() in ("sha256sums", "sha256sums.txt", f"{zip_name}.sha256".lower()):
            url = a.get("browser_download_url", "")
            text = fetch(url, 64_000).decode("utf-8", "replace")
            for line in text.splitlines():
                m = re.match(r"^\s*([0-9a-fA-F]{64})\b\s*\*?(.*)$", line)
                if m and (not m.group(2).strip() or m.group(2).strip().lstrip("*./") == zip_name):
                    return m.group(1).lower()
    m = re.search(rf"{re.escape(zip_name)}\W+(?:sha-?256\W+)?([0-9a-fA-F]{{64}})", release.get("body") or "", re.I)
    return m.group(1).lower() if m else ""


def check_for_update(current: str = __version__, repo: str = UPDATE_REPO,
                     fetch: Fetch = http_get) -> Optional[UpdateInfo]:
    """Return UpdateInfo if a newer release exists, else None. Raises UpdateError on any problem."""
    if not enabled(repo):
        raise UpdateError("Update source is not configured (UPDATE_REPO in appinfo.py).")
    try:
        rel = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest", MAX_API_BYTES))
    except ValueError:
        raise UpdateError("GitHub returned something that is not valid JSON.")
    if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
        return None
    tag = str(rel.get("tag_name", ""))
    if not is_newer(tag, current):
        return None
    page = str(rel.get("html_url", ""))
    page = page if urlparse(page).hostname == "github.com" else f"https://github.com/{repo}/releases"
    assets = [a for a in rel.get("assets", []) if isinstance(a, dict)]
    zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")
            and urlparse(str(a.get("browser_download_url", ""))).scheme == "https"]
    parse_version(tag)                      # raises UpdateError if the tag is not a version
    info = UpdateInfo(version=tag.lstrip("vV"), tag=tag,
                      notes=str(rel.get("body") or "")[:4000], page_url=page)
    if zips:
        z = next((a for a in zips if APP_SLUG in a["name"].lower()), zips[0])
        info.zip_url, info.zip_name = z["browser_download_url"], z["name"]
        try:
            info.sha256 = _find_sha256(rel, z["name"], assets, fetch)
        except UpdateError:
            info.sha256 = ""
    return info


# ---------------------------------------------------------------- install
def is_git_checkout(root: Path) -> bool:
    return (root / ".git").exists()


def download_zip(info: UpdateInfo, fetch: Fetch = http_get) -> bytes:
    if not info.can_self_update:
        raise UpdateError("This release has no checksummed zip asset; install it manually from the release page.")
    data = fetch(info.zip_url, MAX_ZIP_BYTES)
    if hashlib.sha256(data).hexdigest() != info.sha256.lower():
        raise UpdateError("Checksum mismatch: the download does not match the published SHA-256. Nothing was installed.")
    return data


def _safe_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos or len(infos) > MAX_FILES:
        raise UpdateError("Update archive is empty or has too many files.")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise UpdateError("Update archive unpacks to more than the allowed size.")
    paths = []
    for i in infos:
        name = i.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or (p.parts and re.match(r"^[A-Za-z]:", p.parts[0])):
            raise UpdateError(f"Unsafe path in update archive: {i.filename!r}")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise UpdateError(f"Symbolic link in update archive: {i.filename!r}")
        paths.append(p)
    tops = {p.parts[0] for p in paths}
    strip = len(tops) == 1 and all(len(p.parts) > 1 for p in paths)       # GitHub-style single wrapper folder
    out = [(i, PurePosixPath(*p.parts[1:]) if strip else p) for i, p in zip(infos, paths)]
    names = {str(p) for _, p in out}
    if f"{APP_SLUG}/__init__.py" not in names or "run_station.py" not in names:
        raise UpdateError("Update archive does not look like this application.")
    return out


def apply_update(zip_bytes: bytes, root: Path, current_version: str = __version__) -> list[str]:
    """Install a verified zip over `root`. Returns the files written. Backs up replaced files first."""
    if is_git_checkout(root):
        raise UpdateError("This copy is a git checkout. Run `git pull` instead of updating in place.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        if zf.testzip() is not None:
            raise UpdateError("Update archive is corrupt.")
    except zipfile.BadZipFile:
        raise UpdateError("Update archive is not a valid zip file.")
    members = [(i, p) for i, p in _safe_members(zf) if p.parts[0] not in PROTECTED_TOP_LEVEL]
    backup = root / ".update_backup" / f"{current_version}-{datetime.now():%Y%m%d-%H%M%S}"
    written: list[str] = []
    for info, rel in members:
        dest = root.joinpath(*rel.parts)
        if root.resolve() not in dest.resolve().parents:
            raise UpdateError(f"Unsafe destination: {rel}")
        if dest.exists():
            bpath = backup.joinpath(*rel.parts)
            bpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, bpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".update_tmp")
        tmp.write_bytes(zf.read(info))
        os.replace(tmp, dest)
        written.append(str(rel))
    return written
