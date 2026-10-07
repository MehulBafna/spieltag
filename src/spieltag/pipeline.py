"""The ingest step: fetch, keep the raw answer, parse, validate, write the curated table."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from spieltag.contracts import validate_matches
from spieltag.ingest import fetch_season_raw, parse_matches
from spieltag.store import Store


@dataclass(frozen=True)
class IngestResult:
    league: str
    season: int
    snapshot_id: str  # first 12 hex characters of the SHA-256 of the raw payload
    raw_uri: str
    curated_uri: str
    matches: int
    finished: int


def snapshot_id(raw: list[dict[str, Any]]) -> str:
    """Stable ID for a payload: the same data always gives the same ID."""
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def run_ingest(
    league: str,
    season: int,
    store: Store,
    *,
    now: datetime | None = None,
    fetcher: Callable[[str, int], list[dict[str, Any]]] = fetch_season_raw,
) -> IngestResult:
    """Run one ingest. The raw answer is saved first, so bad data can be inspected later.

    The curated table is written only after validation passes. If validation fails, a
    ContractError is raised and nothing curated changes.
    """
    raw = fetcher(league, season)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    raw_uri = store.put_bytes(
        f"raw/openligadb/{league}/{season}/{stamp}.json",
        json.dumps(raw, ensure_ascii=False).encode("utf-8"),
    )

    matches = validate_matches(parse_matches(raw, league_season=season))

    buffer = io.BytesIO()
    matches.to_parquet(buffer, index=False)
    curated_uri = store.put_bytes(
        f"curated/matches/league={league}/season={season}/matches.parquet", buffer.getvalue()
    )

    return IngestResult(
        league=league,
        season=season,
        snapshot_id=snapshot_id(raw),
        raw_uri=raw_uri,
        curated_uri=curated_uri,
        matches=len(matches),
        finished=int(matches["is_finished"].sum()),
    )


def read_curated(store: Store, league: str, season: int) -> pd.DataFrame:
    """Load a curated matches table back from the store."""
    data = store.get_bytes(f"curated/matches/league={league}/season={season}/matches.parquet")
    return pd.read_parquet(io.BytesIO(data))
