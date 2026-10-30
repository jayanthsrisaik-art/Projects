import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slotwise.availability import event_type_from_dict  # noqa: E402
from slotwise.store import Store  # noqa: E402

SAMPLES = ROOT / "samples"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)  # Fri 08:00 New York


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture
def configs():
    return {c["slug"]: c for c in json.loads((SAMPLES / "event_types.json").read_text())}


@pytest.fixture
def ets(configs):
    return {k: event_type_from_dict(v) for k, v in configs.items()}


@pytest.fixture
def store(tmp_path, configs):
    s = Store(str(tmp_path / "t.db"))
    for c in configs.values():
        s.upsert_event_type(c)
    return s
