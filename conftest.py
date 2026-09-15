"""Make `pytest` work from the repo root without a manual PYTHONPATH."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
for path in (ROOT / "src", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
