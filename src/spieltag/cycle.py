"""One production cycle, run on a schedule: ingest, freeze, score, publish.

1. Ingest the current season. If the API is down or the data fails its contract, carry on with the
   data already stored: forecasts can still be frozen, they just know less. The failure is
   reported, not hidden.
2. Freeze a forecast for every match whose time has come (24 hours before kickoff) and that has no
   forecast yet. A forecast is never written after kickoff and never overwritten.
3. Score every frozen forecast whose match has finished.
4. Publish the results as small JSON files for the static dashboard.

If freezing fails, the cycle still publishes the truth (so the dashboard shows the problem) and
reports the error, so the caller can fail loudly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from spieltag.features import DEFAULT_LEAD, build_features
from spieltag.frozen import (
    freeze_due,
    frozen_match_ids,
    iso,
    load_frozen,
    parse_time,
)
from spieltag.ingest import current_season, fetch_season_raw
from spieltag.live import live_summary, score_frozen, to_json, upcoming_fixtures
from spieltag.models import PRIMARY_MODEL, REFERENCE_MODEL, Model, default_models
from spieltag.observability import log_event
from spieltag.pipeline import load_matches, run_ingest
from spieltag.store import Store

DEFAULT_FIRST_SEASON = 2016
DEFAULT_MIN_TRAIN = 600
SITE_DATA_PREFIX = "site/data/"
DATA_CACHE = "public, max-age=60"
MAX_PROBLEMS_SHOWN = 10
MISSED_WINDOW = timedelta(hours=72)  # a miss raises the alarm for this long, then stays on record


class CycleError(RuntimeError):
    """The cycle cannot do anything useful, for example because there is no data at all."""


@dataclass
class CycleResult:
    league: str
    now: str
    season: int
    code_version: str
    go_live_utc: str
    ingest_status: str  # changed, unchanged, empty or failed
    ingest_error: str | None = None
    snapshot_id: str | None = None
    seasons_loaded: list[int] = field(default_factory=list)
    frozen_now: int = 0
    lost_race: int = 0
    waiting_for_history: int = 0
    anomalies: int = 0
    frozen_total: int = 0
    scored: int = 0
    awaiting_result: int = 0
    voided: int = 0
    missed: list[int] = field(default_factory=list)  # kicked off in the last 72 hours, no forecast
    missed_total: int = 0  # every miss since the pipeline went live
    problems: list[str] = field(default_factory=list)
    freeze_error: str | None = None
    published: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return (
            self.freeze_error is None
            and self.ingest_status != "failed"
            and not self.missed
            and not self.problems
        )

    def metrics(self) -> dict[str, tuple[float, str]]:
        count = "Count"
        return {
            "CycleCompleted": (0 if self.freeze_error else 1, count),
            "FrozenThisRun": (self.frozen_now, count),
            "FrozenTotal": (self.frozen_total, count),
            "MatchesScored": (self.scored, count),
            "MissedFreezes": (len(self.missed), count),
            "IngestFailed": (1 if self.ingest_status == "failed" else 0, count),
            "WaitingForHistory": (self.waiting_for_history, count),
            "ProblemRecords": (len(self.problems), count),
        }

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def ensure_go_live(store: Store, league: str, now: datetime, code_version: str) -> pd.Timestamp:
    """The moment the pipeline first ran. Matches that kicked off before it can never be scored.

    Written once. It tells the missed-forecast check which matches the pipeline was responsible
    for, so history that predates the pipeline does not raise false alarms.
    """
    key = f"meta/league={league}/go_live.json"
    body = {"go_live_utc": iso(now), "code_version": code_version}
    store.put_bytes_if_absent(key, json.dumps(body).encode())
    return parse_time(json.loads(store.get_bytes(key))["go_live_utc"])


def _previous_status(store: Store) -> dict[str, Any]:
    try:
        return json.loads(store.get_bytes(f"{SITE_DATA_PREFIX}status.json"))
    except (FileNotFoundError, ValueError):
        return {}


def _publish(store: Store, name: str, payload: bytes) -> str:
    return store.put_bytes(
        f"{SITE_DATA_PREFIX}{name}",
        payload,
        content_type="application/json",
        cache_control=DATA_CACHE,
    )


def run_cycle(
    store: Store,
    *,
    league: str = "bl1",
    now: datetime | None = None,
    fetcher: Callable[[str, int], list[dict[str, Any]]] = fetch_season_raw,
    factories: dict[str, Callable[[], Model]] | None = None,
    first_season: int = DEFAULT_FIRST_SEASON,
    lead: timedelta = DEFAULT_LEAD,
    min_train_matches: int = DEFAULT_MIN_TRAIN,
    code_version: str = "dev",
    backfill: bool = False,
) -> CycleResult:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    season = current_season(now.date())
    go_live = ensure_go_live(store, league, now, code_version)
    result = CycleResult(
        league=league,
        now=iso(now),
        season=season,
        code_version=code_version,
        go_live_utc=iso(go_live),
        ingest_status="unchanged",
    )

    # 1. ingest ------------------------------------------------------------------------------
    to_ingest = range(first_season, season + 1) if backfill else [season]
    try:
        for target in to_ingest:
            ingested = run_ingest(
                league, target, store, now=now, fetcher=fetcher, only_if_changed=True
            )
            if target == season:
                result.snapshot_id = ingested.snapshot_id
                if ingested.changed:
                    result.ingest_status = "changed"
                elif ingested.matches == 0:
                    result.ingest_status = "empty"
    except Exception as exc:  # any failure here must not stop forecasts being frozen
        result.ingest_status = "failed"
        result.ingest_error = f"{type(exc).__name__}: {exc}"
        log_event("ingest_failed", season=season, error=result.ingest_error)

    # 2. load --------------------------------------------------------------------------------
    matches, missing = load_matches(store, league, range(first_season, season + 1))
    if matches.empty:
        raise CycleError(f"no curated matches for {league}; run spieltag-ingest or backfill first")
    result.seasons_loaded = sorted(int(s) for s in matches["season"].unique())
    history_gaps = [s for s in missing if s < season]
    if history_gaps:
        result.problems.append(f"no curated data for seasons {history_gaps}")
    features = build_features(matches, lead=lead)

    # 3. freeze ------------------------------------------------------------------------------
    already = frozen_match_ids(store, league, [season])
    frozen_now: set[int] = set()
    try:
        report = freeze_due(
            store,
            matches,
            features,
            factories or default_models(),
            league=league,
            now=now,
            lead=lead,
            min_train_matches=min_train_matches,
            code_version=code_version,
            already_frozen=already,
        )
        frozen_now = set(report.frozen)
        result.frozen_now = len(report.frozen)
        result.lost_race = len(report.lost_race)
        result.waiting_for_history = len(report.waiting_for_history)
        result.anomalies = len(report.skipped_anomalies)
        if report.skipped_anomalies:
            result.problems.append(
                f"matches marked finished before kickoff: {report.skipped_anomalies}"
            )
    except Exception as exc:  # publish what is true first, then report the failure
        result.freeze_error = f"{type(exc).__name__}: {exc}"
        log_event("freeze_failed", error=result.freeze_error)
        frozen_now = set()

    # 4. missed forecasts: the pipeline was responsible for the match but wrote nothing ---------
    have = frozen_match_ids(store, league, [season]) | frozen_now
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    responsible = matches[(matches["kickoff_utc"] > go_live) & (matches["kickoff_utc"] <= now_ts)]
    unforecast = responsible[~responsible["match_id"].isin(have)]
    result.missed_total = len(unforecast)
    recent = unforecast[unforecast["kickoff_utc"] > now_ts - MISSED_WINDOW]
    result.missed = sorted(int(i) for i in recent["match_id"])
    if result.missed:
        log_event("missed_forecasts", match_ids=result.missed)

    # 5. score -------------------------------------------------------------------------------
    first_frozen_season = current_season(go_live.date())
    records, record_problems = load_frozen(
        store, league, list(range(first_frozen_season, season + 1))
    )
    result.problems += record_problems
    scoring = score_frozen(records, matches)
    result.frozen_total = len(records)
    result.scored = int(scoring.scored["match_id"].nunique()) if not scoring.scored.empty else 0
    result.awaiting_result = len(scoring.pending)
    result.voided = len(scoring.voided)
    if scoring.voided:
        result.problems.append(f"forecasts that cannot count (kickoff moved): {scoring.voided}")
    if scoring.orphans:
        result.problems.append(f"forecasts for matches that left the data: {scoring.orphans}")

    # 6. publish -----------------------------------------------------------------------------
    previous = _previous_status(store)
    last_success = (
        previous.get("ingest", {}).get("last_success_utc")
        if result.ingest_status == "failed"
        else iso(now)
    )
    upcoming = upcoming_fixtures(matches, features, records, now=now)
    unfrozen_due = features[(features["kickoff_utc"] > now_ts) & ~features["match_id"].isin(have)][
        "as_of_utc"
    ]
    next_freeze = unfrozen_due[unfrozen_due > now_ts].min() if not unfrozen_due.empty else pd.NaT
    status = {
        "generated_at": iso(now),
        "league": league,
        "season": season,
        "healthy": result.healthy,
        "go_live_utc": iso(go_live),
        "code_version": code_version,
        "primary_model": PRIMARY_MODEL,
        "reference_model": REFERENCE_MODEL,
        "lead_hours": lead.total_seconds() / 3600,
        "ingest": {
            "status": result.ingest_status,
            "error": result.ingest_error,
            "snapshot_id": result.snapshot_id,
            "last_success_utc": last_success,
        },
        "counts": {
            "frozen_total": result.frozen_total,
            "scored": result.scored,
            "awaiting_result": result.awaiting_result,
            "voided": result.voided,
            "missed": len(result.missed),
            "missed_total": result.missed_total,
        },
        "next_freeze_utc": None if pd.isna(next_freeze) else iso(next_freeze),
        "problems": result.problems[:MAX_PROBLEMS_SHOWN]
        + (
            [f"... and {len(result.problems) - MAX_PROBLEMS_SHOWN} more"]
            if len(result.problems) > MAX_PROBLEMS_SHOWN
            else []
        ),
        "freeze_error": result.freeze_error,
    }
    result.published.append(
        _publish(store, "live.json", to_json(live_summary(scoring, matches, now=now)))
    )
    result.published.append(_publish(store, "upcoming.json", to_json(upcoming)))
    try:
        backtest = store.get_bytes(f"evaluation/{league}/report.json")
        result.published.append(_publish(store, "backtest.json", backtest))
    except FileNotFoundError:
        log_event("no_backtest_report", league=league)
    result.published.append(_publish(store, "status.json", to_json(status)))

    log_event("cycle_done", **{k: v for k, v in result.summary().items() if k != "published"})
    return result
