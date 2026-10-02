import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("USBLOCKBOX_HOME", tempfile.mkdtemp(prefix="usblockbox_test_"))   # tests never write in the repo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usblockbox.backends.simulated import SimulatedBackend, STATION_PW_DEFAULT  # noqa: E402
from usblockbox.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.csv_dir = str(tmp_path / "csv")
    s.pdf_dir = str(tmp_path / "pdf")
    s.set_fixed_password(STATION_PW_DEFAULT)
    s.overwrite_passes = 3
    s.dry_run = False                   # tests exercise the erase path against the simulator
    s.include_secrets_csv = True        # the code path under test; the shipped default is False
    return s


@pytest.fixture
def backend():
    return SimulatedBackend(speed=200.0)
