import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
from pathlib import Path as _Path

SEED_DIR = _Path(__file__).resolve().parents[1] / "seed"


def load_seed(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text())
