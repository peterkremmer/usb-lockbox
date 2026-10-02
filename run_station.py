"""Double-click launcher. Real mode re-launches itself elevated (UAC prompt); simulation does not need admin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usblockbox.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
