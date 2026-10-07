"""The ingest step: fetch, keep the raw answer, parse, validate, write the curated table."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from spieltag.contracts import ContractError, validate_matches
from spieltag.ingest import MATCH_COLUMNS, fetch_season_raw, parse_matches
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
    changed: bool = True  # False when only_if_changed found the same payload as last time


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
    only_if_changed: bool = False,
) -> IngestResult:
    """Run one ingest. The raw answer is saved first, so bad data can be inspected later.

    The curated table is written only after validation passes. If validation fails, a
    ContractError is raised and nothing curated changes.

    With `only_if_changed`, an answer identical to the last stored one writes nothing, and an
    empty answer (fixtures not published yet) is not an error. A scheduled job uses this so that
    most runs leave the data lake untouched.
    """
    raw = fetcher(league, season)
    marker_key = f"curated/matches/league={league}/season={season}/_snapshot.txt"
    if only_if_changed:
        if not raw:
            return IngestResult(league, season, snapshot_id(raw), "", "", 0, 0, changed=False)
        try:
            unchanged = store.get_bytes(marker_key).decode().strip() == snapshot_id(raw)
        except FileNotFoundError:
            unchanged = False
        if unchanged:
            parsed = parse_matches(raw, league_season=season)
            return IngestResult(
                league,
                season,
                snapshot_id(raw),
                "",
                "",
                len(parsed),
                int(parsed["is_finished"].sum()),
                changed=False,
            )

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
    store.put_bytes(marker_key, snapshot_id(raw).encode("ascii"))  # last, so a failure retries

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


def load_matches(
    store: Store, league: str, seasons: Iterable[int]
) -> tuple[pd.DataFrame, list[int]]:
    """Load every available season, oldest first. Returns the table and the seasons not found."""
    frames, missing = [], []
    for season in seasons:
        try:
            frames.append(read_curated(store, league, season))
        except FileNotFoundError:
            missing.append(season)
    if not frames:
        return pd.DataFrame(columns=MATCH_COLUMNS), missing
    matches = pd.concat(frames, ignore_index=True)
    if matches["match_id"].duplicated().any():
        raise ContractError("the same match_id appears in more than one season")
    return matches, missing
