import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flagdeck.app import create_app  # noqa: E402
from flagdeck.schema import FlagIn  # noqa: E402
from flagdeck.seed import seed  # noqa: E402


def load_sample_flags() -> dict[str, dict]:
    docs = json.loads((ROOT / "samples/flags.json").read_text())
    return {d["key"]: FlagIn.model_validate(d).doc() for d in docs}


def flag(key="f", variations=(False, True), prereqs=(), **envs) -> dict:
    """Build a normalised flag doc; ``envs`` maps env name to a partial EnvConfig."""
    return FlagIn.model_validate({"key": key, "variations": list(variations),
                                  "prerequisites": list(prereqs), "environments": envs}).doc()


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "t.db")
    app = create_app(db)
    with TestClient(app) as c:
        c.db_path = db
        yield c


@pytest.fixture
def seeded(client):
    seed(client.db_path, str(ROOT / "samples/flags.json"))
    return client
