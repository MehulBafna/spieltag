import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_sample() -> list[dict]:
    """Four invented matches in the shape OpenLigaDB returns. Not real Bundesliga data."""
    return json.loads((FIXTURES / "openligadb_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def league_matches():
    """Five simulated seasons with real signal in them. Invented data."""
    from league import simulate_league

    return simulate_league(range(2016, 2021), strength_sd=0.5, seed=11)


@pytest.fixture(scope="session")
def noise_matches():
    """Five simulated seasons where all clubs are equal, so nothing can be predicted."""
    from league import simulate_league

    return simulate_league(range(2016, 2021), strength_sd=0.0, seed=11)
