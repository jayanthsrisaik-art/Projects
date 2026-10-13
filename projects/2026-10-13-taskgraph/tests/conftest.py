import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PY = sys.executable


@pytest.fixture
def make_project(tmp_path):
    """Write a tasks.toml (with {py} substituted) and return its path."""
    def _make(body: str) -> Path:
        f = tmp_path / "tasks.toml"
        f.write_text(textwrap.dedent(body).replace("{py}", PY.replace("\\", "/")))
        return f
    return _make
