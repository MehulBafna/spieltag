import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_sample() -> list[dict]:
    """Four invented matches in the shape OpenLigaDB returns. Not real Bundesliga data."""
    return json.loads((FIXTURES / "openligadb_sample.json").read_text(encoding="utf-8"))
