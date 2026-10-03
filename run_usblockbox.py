"""Launcher for USB Lockbox (same as `py -m usblockbox`). On Windows it re-launches itself elevated (UAC prompt)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usblockbox.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
