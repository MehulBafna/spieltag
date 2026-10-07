"""Frozen predictions: written once, before kickoff, and never changed.

A forecast only means something if it was made before the result was known. So the pipeline writes
each match's forecast to its own object with a conditional write (`put_bytes_if_absent`). A second
write for the same match is refused, even if two runs overlap, and the object carries its own
fingerprint so a later edit is detectable. Every record also stores what it was made from: the
cut-off of its training data, a fingerprint of that data, and the code version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from spieltag.evaluate import round_starts, training_window
from spieltag.features import FEATURE_COLUMNS
from spieltag.metrics import PROBABILITY_COLUMNS
from spieltag.models import PRIMARY_MODEL, Model
from spieltag.store import Store

SCHEMA_VERSION = 1


def frozen_prefix(league: str, season: int | None = None) -> str:
    base = f"predictions/league={league}/"
    return base if season is None else f"{base}season={season}/"


def frozen_key(league: str, season: int, match_id: int) -> str:
    return f"{frozen_prefix(league, season)}match_id={match_id}.json"


def iso(moment: Any) -> str:
    """A UTC time as text, to the second, e.g. 2026-10-10T13:30:00Z."""
    return pd.Timestamp(moment).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> pd.Timestamp:
    return pd.Timestamp(text).tz_convert("UTC")


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(record: dict[str, Any]) -> str:
    """SHA-256 of the record without its own fingerprint field."""
    body = {key: value for key, value in record.items() if key != "fingerprint"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def data_fingerprint(train: pd.DataFrame) -> str:
    """Short fingerprint of the exact training results a forecast was made from."""
    rows = train.sort_values("match_id")[["match_id", "home_goals", "away_goals"]]
    digest = hashlib.sha256(rows.to_csv(index=False).encode()).hexdigest()
    return digest[:12]


def build_record(
    *,
    league: str,
    row: pd.Series,
    names: tuple[str, str],
    probabilities: dict[str, tuple[float, float, float]],
    frozen_at: datetime,
    trained_until: pd.Timestamp,
    train: pd.DataFrame,
    lead: timedelta,
    code_version: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "league": league,
        "season": int(row["season"]),
        "matchday": int(row["matchday"]),
        "match_id": int(row["match_id"]),
        "home_team": names[0],
        "away_team": names[1],
        "kickoff_utc": iso(row["kickoff_utc"]),
        "as_of_utc": iso(row["as_of_utc"]),
        "frozen_at_utc": iso(frozen_at),
        "lead_hours": lead.total_seconds() / 3600,
        "primary_model": PRIMARY_MODEL,
        "models": {
            name: dict(zip(PROBABILITY_COLUMNS, (float(p) for p in values), strict=True))
            for name, values in probabilities.items()
        },
        "features": {column: round(float(row[column]), 6) for column in FEATURE_COLUMNS},
        "training": {
            "trained_until": iso(trained_until),
            "matches": len(train),
            "data_fingerprint": data_fingerprint(train),
        },
        "code_version": code_version,
    }
    record["fingerprint"] = fingerprint(record)
    return record


def check_record(record: dict[str, Any]) -> list[str]:
    """What is wrong with a stored record. Empty if it is sound."""
    problems = []
    try:
        if record.get("schema_version") != SCHEMA_VERSION:
            problems.append(f"unknown schema version {record.get('schema_version')!r}")
        if record.get("fingerprint") != fingerprint(record):
            problems.append("fingerprint does not match: the record was changed after it was made")
        if parse_time(record["frozen_at_utc"]) >= parse_time(record["kickoff_utc"]):
            problems.append("frozen at or after kickoff")
        for name, probabilities in record["models"].items():
            values = [probabilities[column] for column in PROBABILITY_COLUMNS]
            if min(values) < 0 or not np.isclose(sum(values), 1.0, atol=1e-6):
                problems.append(f"{name}: probabilities are not a distribution")
        if record["primary_model"] not in record["models"]:
            problems.append("primary model missing")
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"malformed record: {type(exc).__name__}: {exc}")
    return problems


@dataclass
class FreezeReport:
    frozen: list[int] = field(default_factory=list)  # match ids written in this run
    lost_race: list[int] = field(default_factory=list)  # already written by an overlapping run
    waiting_for_history: list[int] = field(default_factory=list)  # too little training data
    skipped_anomalies: list[int] = field(default_factory=list)  # unplayed match marked finished


def frozen_match_ids(store: Store, league: str, seasons: list[int]) -> set[int]:
    """Match ids that already have a frozen record, read from object names alone."""
    ids: set[int] = set()
    for season in seasons:
        for key in store.list_keys(frozen_prefix(league, season)):
            stem = key.rsplit("/", 1)[-1]
            if stem.startswith("match_id=") and stem.endswith(".json"):
                ids.add(int(stem[len("match_id=") : -len(".json")]))
    return ids


def due_matches(
    features: pd.DataFrame, already_frozen: set[int], now: datetime
) -> tuple[pd.DataFrame, list[int]]:
    """Matches whose time has come: `as_of` has passed, kickoff has not, nothing frozen yet.

    Also returns ids that are due but contradictory (marked finished before they kicked off).
    """
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    window = features[(features["as_of_utc"] <= now_ts) & (features["kickoff_utc"] > now_ts)]
    window = window[~window["match_id"].isin(already_frozen)]
    anomalies = window["is_finished"].astype(bool)
    return window[~anomalies], [int(i) for i in window.loc[anomalies, "match_id"]]


def freeze_due(
    store: Store,
    matches: pd.DataFrame,
    features: pd.DataFrame,
    factories: dict[str, Callable[[], Model]],
    *,
    league: str,
    now: datetime,
    lead: timedelta,
    min_train_matches: int,
    code_version: str,
    already_frozen: set[int],
) -> FreezeReport:
    """Fit the models and freeze a forecast for every match that is due.

    A matchday's models are trained on the results known at `first kickoff - lead`, exactly as in
    the backtest (`walk_forward`). Each match's features were read off at its own `as_of` time, so a
    forecast written late (a missed run) is identical to one written on time.
    """
    report = FreezeReport()
    due, anomalies = due_matches(features, already_frozen, now)
    report.skipped_anomalies = anomalies
    if due.empty:
        return report

    starts = round_starts(features)
    names = matches.set_index("match_id")[["home_team", "away_team"]]
    for (season, matchday), games in due.groupby(["season", "matchday"], sort=True):
        trained_until = starts[(season, matchday)] - lead
        train = training_window(features, trained_until)
        if len(train) < min_train_matches:
            report.waiting_for_history += [int(i) for i in games["match_id"]]
            continue

        predicted = {
            name: factory().fit(train).predict_proba(games) for name, factory in factories.items()
        }
        for position, (_, row) in enumerate(games.iterrows()):
            record = build_record(
                league=league,
                row=row,
                names=(
                    str(names.at[row["match_id"], "home_team"]),
                    str(names.at[row["match_id"], "away_team"]),
                ),
                probabilities={name: tuple(values[position]) for name, values in predicted.items()},
                frozen_at=now,
                trained_until=trained_until,
                train=train,
                lead=lead,
                code_version=code_version,
            )
            written = store.put_bytes_if_absent(
                frozen_key(league, int(season), int(row["match_id"])),
                json.dumps(record, indent=2, sort_keys=True).encode("utf-8"),
            )
            (report.frozen if written else report.lost_race).append(int(row["match_id"]))
    return report


def load_frozen(
    store: Store, league: str, seasons: list[int], *, workers: int = 16
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every frozen record of the given seasons. Returns the sound ones and the problems."""
    keys = [key for season in seasons for key in store.list_keys(frozen_prefix(league, season))]

    def read(key: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            return key, json.loads(store.get_bytes(key)), None
        except (FileNotFoundError, ValueError) as exc:
            return key, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(read, keys))

    records, problems = [], []
    for key, record, error in results:
        if record is None:
            problems.append(f"{key}: unreadable ({error})")
            continue
        issues = check_record(record)
        if issues:
            problems.append(f"{key}: {'; '.join(issues)}")
        else:
            records.append(record)
    return records, problems
