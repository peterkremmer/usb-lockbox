"""USB 2.0 warning for big drives, and reading a port's connection speed."""
import struct

from usblockbox import usbports
from usblockbox.config import Settings
from usblockbox.models import DriveInfo
from usblockbox.scan import scan_drive, slow_link_finding


def _drive(gb, speed):
    return DriveInfo(disk_number=3, unique_id="u", serial="S1", size_bytes=int(gb * 1e9), link_speed=speed)


def test_big_drive_on_usb2_gets_a_note_with_a_time():
    f = slow_link_finding(_drive(248, 2), passes=3)
    assert f and f.code == "SLOW_LINK" and "USB 2.0" in f.message and "248 GB" in f.message
    assert "2.3 h" in f.message and "9.2 h" in f.message          # 248 GB at 30 MB/s = 2.3 h per write, 4 writes


def test_no_note_for_fast_small_or_unknown_links():
    assert slow_link_finding(_drive(248, 3), 3) is None          # SuperSpeed
    assert slow_link_finding(_drive(248, -1), 3) is None         # unknown: say nothing rather than guess
    assert slow_link_finding(_drive(32, 2), 3) is not None       # 32 GB, 3 passes: over an hour in all
    assert slow_link_finding(_drive(32, 2), 0) is None           # no overwrite: about 36 minutes
    assert slow_link_finding(_drive(8, 2), 3) is None            # small drive: short either way
    assert slow_link_finding(_drive(128, 2), 0) is not None      # 1.2 h for even one write


def test_note_is_information_only_and_changes_no_verdict():
    s = Settings()
    slow = scan_drive(_drive(248, 2), s, set())
    fast = scan_drive(_drive(248, 3), s, set())
    assert any(f.code == "SLOW_LINK" for f in slow.findings)
    assert slow.verdict == fast.verdict
    assert all(f.severity.value == "INFO" for f in slow.findings if f.code == "SLOW_LINK")


def _answer(speed, status=1):
    b = bytearray(64)
    b[23] = speed
    struct.pack_into("<I", b, 31, status)
    return bytes(b)


def test_parse_connection_speed():
    assert usbports.parse_connection_speed(_answer(2)) == 2
    assert usbports.parse_connection_speed(_answer(3)) == 3
    assert usbports.parse_connection_speed(_answer(2, status=0)) is None   # nothing connected
    assert usbports.parse_connection_speed(b"\0" * 10) is None
    assert "2.0" in usbports.speed_name(2) and "3" in usbports.speed_name(3)


def test_port_of_matches_only_the_right_hub():
    hub = "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(1)"
    assert usbports.port_of(hub + "#USB(3)", hub) == 3
    assert usbports.port_of("PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(1)", "PCIROOT(0)#PCI(1400)#USBROOT(0)") == 1
    assert usbports.port_of(hub + "#USB(3)", "PCIROOT(0)#PCI(1400)#USBROOT(0)") is None   # a port of the hub below
    assert usbports.port_of("x", "") is None


def test_scanner_reads_speed_of_the_right_hub_and_never_raises():
    sc = usbports.PortScanner(lambda *a, **k: "")
    root = "PCIROOT(0)#PCI(1400)#USBROOT(0)"
    sc._hubs = [usbports.HubRaw("a", "i1", root, 4, {}, "\\\\?\\hubA"),
                usbports.HubRaw("b", "i2", root + "#USB(1)", 4, {}, "\\\\?\\hubB")]
    seen = []
    sc.speed_reader = lambda path, n: (seen.append((path, n)), 2)[1]
    assert sc.port_speed(root + "#USB(1)#USB(3)") == 2
    assert seen == [("\\\\?\\hubB", 3)]

    def boom(path, n):
        raise OSError("no")
    sc.speed_reader = boom
    assert sc.port_speed(root + "#USB(2)") is None
    assert sc.port_speed("") is None
