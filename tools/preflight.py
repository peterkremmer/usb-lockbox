"""USB Lockbox launcher check: is Python usable, and are the required packages present?

run_usblockbox.bat runs this before starting the app. It uses only the standard library and avoids
newer Python syntax, so even an old Python can run it and explain what is wrong.

  python tools/preflight.py                      full check; offers to install missing packages
  python tools/preflight.py --python-only        quiet; exit code only (used to pick a Python)
  python tools/preflight.py --check-requirements quiet; exit code only
  python tools/preflight.py --install-requirements  (internal) pip install in an administrator window

Exit codes: 0 ready, 10 Python unsuitable, 20 packages missing or out of date.
"""
import io
import os
import re
import struct
import subprocess
import sys
import time

MIN_PY = (3, 10)
MAX_PY_EXCLUSIVE = (3, 15)            # PySide6 wheels exist for 3.10 to 3.14
OVERRIDE_ENV = "USBLOCKBOX_ALLOW_USER_PYTHON"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")

INSTALL_HELP = """
How to install Python for ALL USERS:
  1. Open https://www.python.org/downloads/windows/ and download the
     "Windows installer (64-bit)" for Python 3.13 or 3.14.
  2. Run it. On the first screen tick "Add python.exe to PATH", then click
     "Customize installation".
  3. Keep the default options and click Next. On the "Advanced Options" page tick
     "Install Python for all users" (the wording may differ slightly), then Install.
     Windows will ask you to approve administrator access.
  4. When it finishes, double-click run_usblockbox.bat again. No restart is needed.
"""


# ---------------------------------------------------------------- pure logic (unit-tested)
def _norm(path):
    return path.replace("\\", "/").rstrip("/").lower()


def is_per_user_path(path, env=None):
    """True when `path` is inside the signed-in user's profile or is a Microsoft Store / MSIX Python."""
    env = os.environ if env is None else env
    p = _norm(path)
    if "/windowsapps/" in p + "/" or "/packages/pythonsoftwarefoundation" in p:
        return True
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = env.get(key)
        if base and (p == _norm(base) or p.startswith(_norm(base) + "/")):
            return True
    return False


def python_problems(version=None, bits=None, prefixes=None, windows=None, env=None):
    """List of (code, message) for everything wrong with this interpreter. Empty list means fine."""
    version = tuple(sys.version_info[:2]) if version is None else tuple(version)[:2]
    bits = struct.calcsize("P") * 8 if bits is None else bits
    windows = (os.name == "nt") if windows is None else windows
    env = os.environ if env is None else env
    prefixes = [sys.prefix, getattr(sys, "base_prefix", sys.prefix)] if prefixes is None else prefixes
    out = []
    if version < MIN_PY or version >= MAX_PY_EXCLUSIVE:
        out.append(("VERSION", "Python %d.%d is not supported. USB Lockbox needs Python %d.%d to %d.%d."
                    % (version[0], version[1], MIN_PY[0], MIN_PY[1], MAX_PY_EXCLUSIVE[0], MAX_PY_EXCLUSIVE[1] - 1)))
    if bits != 64:
        out.append(("BITS", "This is %d-bit Python. USB Lockbox needs the 64-bit version." % bits))
    if windows and env.get(OVERRIDE_ENV) != "1":
        for p in prefixes:
            if p and is_per_user_path(p, env):
                out.append(("PER_USER",
                            "This Python is installed for your user only (%s). The app asks Windows for "
                            "administrator rights, and the administrator account may not be able to use a "
                            "per-user Python or its packages. Install Python for all users instead." % p))
                break
    return out


def norm_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", text)[:4])


def parse_requirements(text):
    """[(name, operator, version)] from a simple requirements.txt. Unknown operators are checked by pip only."""
    reqs = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:(>=|==)\s*([0-9][0-9A-Za-z.\-+]*))?", line)
        if m:
            reqs.append((m.group(1), m.group(2) or "", m.group(3) or ""))
    return reqs


# A package that provides everything the app imports under another name (the app only needs Qt's core modules).
ALIASES = {"pyside6": ("pyside6-essentials",)}


def _lookup(versions, key):
    for k in (key,) + ALIASES.get(key, ()):
        if k in versions:
            return versions[k]
    return None


def requirement_problems(reqs, system_versions, user_versions):
    """[(name, wanted, state, found)] with state 'missing', 'user-only' or 'outdated'. Keys are normalised names."""
    out = []
    for name, op, wanted in reqs:
        key = norm_name(name)
        want = (op + wanted) if op else "any version"
        have = _lookup(system_versions, key)
        if have is None:
            found = _lookup(user_versions, key)
            out.append((name, want, "user-only" if found is not None else "missing", found or ""))
        elif op == ">=" and version_tuple(have) < version_tuple(wanted):
            out.append((name, want, "outdated", have))
        elif op == "==" and version_tuple(have) != version_tuple(wanted):
            out.append((name, want, "outdated", have))
    return out


# ---------------------------------------------------------------- environment readers
def _user_site_dirs():
    dirs = []
    try:
        import site
        for d in (site.getusersitepackages(), getattr(site, "USER_SITE", None)):
            if d:
                dirs.append(d)
    except Exception:   # noqa: BLE001
        pass
    return dirs


def installed_versions():
    """(system, user_only): versions seen on the normal search path WITHOUT the per-user site folder, and in it.

    Packages only in the user's own site folder are invisible to an elevated session that runs as another
    account, so they do not count as installed."""
    import importlib.metadata as md
    user_dirs = [_norm(d) for d in _user_site_dirs()]
    system_paths = [p for p in sys.path if p and _norm(p) not in user_dirs]

    def collect(paths):
        found = {}
        for dist in md.distributions(path=paths):
            try:
                found.setdefault(norm_name(dist.metadata["Name"]), dist.version)
            except Exception:   # noqa: BLE001
                continue
        return found

    return collect(system_paths), collect(_user_site_dirs())


def current_requirement_problems():
    with open(REQUIREMENTS, "r") as f:
        reqs = parse_requirements(f.read())
    system, user = installed_versions()
    return requirement_problems(reqs, system, user), system


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:   # noqa: BLE001
        return False


# ---------------------------------------------------------------- launcher log
def note(message):
    """One line in <data folder>/logs/launcher.log so a support request shows what the launcher found.
    Best effort and capped at 200 KB; the app writes its own log (usblockbox.log) in the same folder."""
    home = os.environ.get("USBLOCKBOX_HOME") or ROOT
    candidates = [os.path.join(home, "data", "logs")]
    if os.environ.get("APPDATA"):
        candidates.append(os.path.join(os.environ["APPDATA"], "usblockbox", "logs"))
    for folder in candidates:
        try:
            if not os.path.isdir(folder):
                os.makedirs(folder)
            path = os.path.join(folder, "launcher.log")
            if os.path.exists(path) and os.path.getsize(path) > 200000:
                os.replace(path, path + ".1")
            with io.open(path, "a", encoding="utf-8") as f:
                f.write(u"%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
            return
        except Exception:   # noqa: BLE001
            continue


# ---------------------------------------------------------------- actions
def install_requirements(wait):
    """pip install -r requirements.txt into this Python's own (system) site-packages."""
    print("Installing the packages USB Lockbox needs (this can take a few minutes)...")
    print("")
    env = dict(os.environ)
    env["PIP_USER"] = "0"                      # never fall back to the per-user folder
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                          "-r", REQUIREMENTS], env=env)
    print("")
    print("Finished successfully." if rc == 0 else "pip reported an error (code %d). Read the messages above." % rc)
    if wait:
        try:
            input("Press Enter to close this window...")
        except EOFError:
            pass
    return rc


def install_with_admin_approval():
    """Run the install in a new window as administrator (UAC prompt) and wait for it to finish."""
    script = os.path.abspath(__file__)
    args = ['"%s"' % script, "--install-requirements"]
    ps_args = ",".join("'%s'" % a.replace("'", "''") for a in args)
    ps = ("$ErrorActionPreference='Stop'; Start-Process -FilePath '%s' -ArgumentList %s -Verb RunAs -Wait"
          % (sys.executable.replace("'", "''"), ps_args))
    return subprocess.call(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps]) == 0


def print_python_help(problems):
    print("")
    for _code, message in problems:
        print("  [!!] " + message)
    print(INSTALL_HELP)
    if any(code == "PER_USER" for code, _m in problems):
        print("If you sign in as an administrator and want to use this Python anyway, run")
        print("    set %s=1" % OVERRIDE_ENV)
        print("in this window and start the app again from here.")
        print("")


def main(argv):
    if "--install-requirements" in argv:
        return install_requirements(wait=True)

    problems = python_problems()
    if "--python-only" in argv:
        return 10 if problems else 0

    if "--check-requirements" in argv:
        try:
            return 20 if current_requirement_problems()[0] else 0
        except Exception:   # noqa: BLE001
            return 20

    print("USB Lockbox setup check")
    where = sys.base_prefix if hasattr(sys, "base_prefix") else sys.prefix
    print("  Python %d.%d.%d (%d-bit): %s" % (sys.version_info[0], sys.version_info[1], sys.version_info[2],
                                              struct.calcsize("P") * 8, where))
    note("Python %d.%d.%d %d-bit at %s (%s): %s" % (
        sys.version_info[0], sys.version_info[1], sys.version_info[2], struct.calcsize("P") * 8, where,
        sys.executable, ", ".join(c for c, _m in problems) or "OK"))
    if problems:
        print_python_help(problems)
        return 10
    print("  [OK] Python version, 64-bit, installed for all users")

    try:
        pkg_problems, system = current_requirement_problems()
    except Exception as e:   # noqa: BLE001
        print("  [!!] Could not read the package list: %s: %s" % (type(e).__name__, e))
        pkg_problems, system = [("(unknown)", "", "missing", "")], {}
    if not pkg_problems:
        print("  [OK] Required packages are installed")
        note("Packages: OK")
        return 0
    note("Packages: " + "; ".join("%s %s %s" % (n, w, s) for n, w, s, _f in pkg_problems))

    print("")
    for name, want, state, found in pkg_problems:
        if state == "user-only":
            print("  [!!] %s %s: found only in your user folder (%s); the elevated app cannot use it" % (name, want, found))
        elif state == "outdated":
            print("  [!!] %s %s: version %s is too old" % (name, want, found))
        else:
            print("  [!!] %s %s: not installed" % (name, want))
    print("")
    try:
        answer = input("Install or update them now? Windows will ask you to approve administrator access. [Y/n] ")
    except EOFError:
        answer = "n"
    if answer.strip().lower() in ("n", "no"):
        note("Install declined by the user")
        print("")
        print("To do it yourself, open Command Prompt or PowerShell as administrator and run:")
        print('    "%s" -m pip install -r "%s"' % (sys.executable, REQUIREMENTS))
        return 20

    note("Installing packages (%s)" % ("already administrator" if is_admin() else "asking for administrator approval"))
    if is_admin():
        install_requirements(wait=False)
    elif not install_with_admin_approval():
        note("Install window did not start (administrator approval declined?)")
        print("")
        print("The install window did not start (the administrator prompt may have been declined).")
        print("Open Command Prompt as administrator and run:")
        print('    "%s" -m pip install -r "%s"' % (sys.executable, REQUIREMENTS))
        return 20

    # Check again in a fresh process so nothing cached from before the install is trusted.
    if subprocess.call([sys.executable, os.path.abspath(__file__), "--check-requirements"]) == 0:
        print("  [OK] Required packages are installed")
        note("Packages installed OK")
        return 0
    note("Packages still missing or out of date after the install")
    print("")
    print("  [!!] The packages are still missing or out of date. Read the install window's messages,")
    print("       fix the problem (for example a network or proxy error) and start the app again.")
    return 20


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
