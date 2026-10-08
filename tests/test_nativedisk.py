"""Reading partition layouts from the raw Windows answer (no Windows needed for the parsing)."""
import struct
import uuid

import pytest

from usblockbox import nativedisk as nd
from usblockbox.backends import windows as w

BASIC = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
RESERVED = "e3c9e316-0b5c-4db8-817d-f92df00215ae"


def _layout(style, entries):
    buf = bytearray(48 + 144 * max(len(entries), 1))
    struct.pack_into("<II", buf, 0, style, len(entries))
    for i, e in enumerate(entries):
        o = 48 + i * 144
        struct.pack_into("<I", buf, o, e["style"])
        struct.pack_into("<q", buf, o + 16, e["length"])
        struct.pack_into("<I", buf, o + 24, e["number"])
        if e["style"] == 1:
            buf[o + 32:o + 48] = uuid.UUID(e["guid"]).bytes_le
            struct.pack_into("<Q", buf, o + 64, e.get("attrs", 0))
        else:
            buf[o + 32] = e.get("ptype", 0)
            buf[o + 33] = e.get("boot", 0)
    return bytes(buf)


def test_gpt_layout():
    style, parts = nd.parse_layout(_layout(1, [
        dict(style=1, length=16 << 20, number=1, guid=RESERVED),
        dict(style=1, length=5_000_000_000, number=2, guid=BASIC, attrs=nd.GPT_HIDDEN)]))
    assert style == "GPT"
    assert parts == [{"Number": 1, "Type": "Reserved", "Size": 16 << 20, "Active": False, "Hidden": False},
                     {"Number": 2, "Type": "Basic", "Size": 5_000_000_000, "Active": False, "Hidden": True}]


def test_mbr_layout_skips_empty_slots():
    style, parts = nd.parse_layout(_layout(0, [
        dict(style=0, length=8_000_000_000, number=1, ptype=0x0C, boot=1),
        dict(style=0, length=0, number=0)]))
    assert style == "MBR" and parts == [{"Number": 1, "Type": "FAT32", "Size": 8_000_000_000, "Active": True, "Hidden": False}]


def test_raw_and_short_answers():
    assert nd.parse_layout(_layout(2, []))[0] == "RAW"
    with pytest.raises(ValueError):
        nd.parse_layout(b"\0" * 10)


def test_off_by_default_and_only_windows(monkeypatch):
    monkeypatch.delenv("USBLOCKBOX_NATIVE_DISKS", raising=False)
    assert not nd.enabled()
    monkeypatch.setenv("USBLOCKBOX_NATIVE_DISKS", "1")
    assert nd.enabled()
    with pytest.raises(OSError):
        nd.native_layout(1)                                  # not Windows here


def test_listing_uses_the_direct_read_and_falls_back(monkeypatch):
    import json
    seen = []

    def ps(script, env=None, timeout=120, label=""):
        seen.append(dict(env or {}))
        return json.dumps([{"Number": 5, "PnpId": "x", "Parts": ["ps"], "Vols": ["ps"], "Style": "PSSTYLE"}])

    monkeypatch.setattr(w, "run_ps", ps)
    monkeypatch.setattr(w.usbports, "native_disk_location", lambda pnp: ("PCIROOT(0)#USBROOT(0)#USB(1)", ""))
    monkeypatch.setenv("USBLOCKBOX_NATIVE_DISKS", "1")
    be = w.WindowsBackend.__new__(w.WindowsBackend)
    monkeypatch.setattr(w.nativedisk, "native_parts_and_volumes", lambda n: ("GPT", ["n"], ["n"]))
    rows = be._list_rows(5)
    assert rows[0]["Parts"] == ["n"] and rows[0]["Style"] == "GPT" and seen[0].get("USBLOCKBOX_NOPARTS") == "1"
    seen.clear()

    def boom(n):
        raise OSError("no")
    monkeypatch.setattr(w.nativedisk, "native_parts_and_volumes", boom)
    rows = be._list_rows(5)
    assert rows[0]["Parts"] == ["ps"] and "USBLOCKBOX_NOPARTS" not in seen[-1]       # PowerShell answered instead
    monkeypatch.delenv("USBLOCKBOX_NATIVE_DISKS")
    seen.clear(); be._list_rows(5)
    assert "USBLOCKBOX_NOPARTS" not in seen[0]
