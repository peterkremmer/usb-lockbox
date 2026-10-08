"""The icon files ship with the app and are real images (pure file checks; Qt is not needed)."""
import struct
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "usblockbox" / "assets"


def test_ico_has_the_usual_sizes():
    data = (ASSETS / "icon.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1) and count >= 6
    sizes = {(data[6 + 16 * i] or 256) for i in range(count)}
    assert {16, 32, 48, 256} <= sizes


def test_png_is_a_png():
    data = (ASSETS / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (256, 256)
