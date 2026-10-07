"""Tests for the launcher check (tools/preflight.py): pure logic only, no installing anything."""
import importlib.util
import json
from pathlib import Path

from usblockbox.usbports import PortScanner

_spec = importlib.util.spec_from_file_location("preflight", Path(__file__).resolve().parents[1] / "tools" / "preflight.py")
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)

ENV = {"LOCALAPPDATA": r"C:\Users\Pat\AppData\Local", "APPDATA": r"C:\Users\Pat\AppData\Roaming",
       "USERPROFILE": r"C:\Users\Pat"}


def codes(**kw):
    kw.setdefault("env", ENV)
    kw.setdefault("windows", True)
    kw.setdefault("bits", 64)
    kw.setdefault("version", (3, 13))
    kw.setdefault("prefixes", [r"C:\Program Files\Python313"])
    return [c for c, _m in pf.python_problems(**kw)]


def test_supported_system_python_is_fine():
    assert codes() == []
    assert codes(version=(3, 10)) == [] and codes(version=(3, 14)) == []


def test_unsupported_versions_and_32_bit():
    assert codes(version=(3, 9)) == ["VERSION"]
    assert codes(version=(3, 15)) == ["VERSION"]
    assert codes(bits=32) == ["BITS"]


def test_per_user_pythons_are_flagged():
    for prefix in (r"C:\Users\Pat\AppData\Local\Programs\Python\Python313",
                   r"C:\Users\Pat\AppData\Local\Python\pythoncore-3.13-64",
                   r"C:\Users\Pat\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_x",
                   r"c:\users\pat\anaconda3"):
        assert codes(prefixes=[prefix]) == ["PER_USER"], prefix


def test_all_users_locations_are_not_flagged():
    for prefix in (r"C:\Program Files\Python313", r"C:\Python313", r"D:\Tools\Python", r"C:\Users\Patrick\Python"):
        assert codes(prefixes=[prefix]) == [], prefix     # 'Patrick' must not match the profile 'Pat'


def test_per_user_check_can_be_overridden_and_is_windows_only():
    per_user = [r"C:\Users\Pat\AppData\Local\Programs\Python\Python313"]
    assert codes(prefixes=per_user, env=dict(ENV, USBLOCKBOX_ALLOW_USER_PYTHON="1")) == []
    assert codes(prefixes=per_user, windows=False) == []


def test_parse_requirements_ignores_comments_options_and_blank_lines():
    text = "# comment\n-r other.txt\n\nPySide6>=6.6\nreportlab >= 4.0  # pdf\nsomething\npinned==1.2.3\n"
    assert pf.parse_requirements(text) == [("PySide6", ">=", "6.6"), ("reportlab", ">=", "4.0"),
                                           ("something", "", ""), ("pinned", "==", "1.2.3")]


def test_version_comparison_is_numeric_not_textual():
    assert pf.version_tuple("6.11.2") > pf.version_tuple("6.6")
    assert pf.version_tuple("10.0") > pf.version_tuple("9.9.9")


def test_requirement_problems_missing_outdated_and_user_only():
    reqs = pf.parse_requirements("PySide6>=6.6\nreportlab>=4.0\nother>=1.0\nfine>=1.0")
    system = {"pyside6": "6.5.0", "fine": "2.0"}
    user = {"reportlab": "5.0.1"}
    got = {name: state for name, _w, state, _f in pf.requirement_problems(reqs, system, user)}
    assert got == {"PySide6": "outdated", "reportlab": "user-only", "other": "missing"}


def test_requirements_that_are_met_report_nothing():
    reqs = pf.parse_requirements("PySide6>=6.6\nreportlab>=4.0")
    assert pf.requirement_problems(reqs, {"pyside6": "6.11.2", "reportlab": "5.0.1"}, {}) == []


def test_name_normalisation():
    assert pf.norm_name("PySide6_Essentials") == pf.norm_name("pyside6.essentials") == "pyside6-essentials"


def test_shipped_requirements_file_parses():
    text = (Path(pf.REQUIREMENTS)).read_text()
    names = {n.lower() for n, _o, _v in pf.parse_requirements(text)}
    assert {"pyside6", "reportlab"} <= names


def test_hub_location_lookup_uses_one_powershell_call():
    calls = []

    def fake_ps(script, env=None, timeout=0):
        calls.append(env)
        return json.dumps([{"Id": "USB\\ROOT_HUB30\\A", "Paths": ["PCIROOT(0)#PCI(1)#USBROOT(0)"]}])

    got = PortScanner(fake_ps)._locations(["USB\\ROOT_HUB30\\A", "USB\\VID_1&PID_2\\B"])
    assert len(calls) == 1
    assert got == {"usb\\root_hub30\\a": "PCIROOT(0)#PCI(1)#USBROOT(0)"}


def test_pyside6_essentials_satisfies_pyside6():
    reqs = pf.parse_requirements("PySide6>=6.6")
    assert pf.requirement_problems(reqs, {"pyside6-essentials": "6.11.2"}, {}) == []
    assert pf.requirement_problems(reqs, {"pyside6-essentials": "6.2.0"}, {})[0][2] == "outdated"
    assert pf.requirement_problems(reqs, {}, {"pyside6-essentials": "6.11.2"})[0][2] == "user-only"
