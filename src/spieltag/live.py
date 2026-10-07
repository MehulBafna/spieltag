"""The live scoreboard: score the frozen forecasts against real results and shape the site data.

Only a sound frozen record can count, and only if it was frozen before the match actually kicked
off. Everything the dashboard shows is computed here, once, into small JSON files, so the page
itself is just static files that read them.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from spieltag.features import OUTCOME_LABELS
from spieltag.frozen import iso, parse_time
from spieltag.metrics import PROBABILITY_COLUMNS, paired_difference, score_predictions, summarize
from spieltag.models import PRIMARY_MODEL, REFERENCE_MODEL

MIN_MATCHES_FOR_VERDICT = 30
UPCOMING_LIMIT = 18
RECENT_LIMIT = 15


@dataclass
class Scoring:
    scored: pd.DataFrame  # one row per frozen match and model, with rps, log_loss, brier, correct
    pending: list[dict[str, Any]] = field(default_factory=list)  # frozen, result not in yet
    voided: list[int] = field(default_factory=list)  # kickoff moved before the freeze time
    orphans: list[int] = field(default_factory=list)  # frozen, but the match left the data


def _outcome(home_goals: int, away_goals: int) -> int:
    return 0 if home_goals > away_goals else (1 if home_goals == away_goals else 2)


def score_frozen(records: list[dict[str, Any]], matches: pd.DataFrame) -> Scoring:
    """Join frozen forecasts to what happened and score the ones that count."""
    by_id = matches.set_index("match_id")
    rows: list[dict[str, Any]] = []
    scoring = Scoring(scored=pd.DataFrame())
    for record in records:
        match_id = int(record["match_id"])
        if match_id not in by_id.index:
            scoring.orphans.append(match_id)
            continue
        match = by_id.loc[match_id]
        if not bool(match["is_finished"]):
            scoring.pending.append(record)
            continue
        if parse_time(record["frozen_at_utc"]) >= match["kickoff_utc"]:
            scoring.voided.append(match_id)
            continue
        outcome = _outcome(int(match["home_goals"]), int(match["away_goals"]))
        for model, probabilities in record["models"].items():
            rows.append(
                {
                    "match_id": match_id,
                    "season": int(record["season"]),
                    "matchday": int(record["matchday"]),
                    "kickoff_utc": match["kickoff_utc"],
                    "model": model,
                    **{column: float(probabilities[column]) for column in PROBABILITY_COLUMNS},
                    "outcome": outcome,
                }
            )
    if rows:
        scoring.scored = score_predictions(pd.DataFrame(rows))
    return scoring


def _clean(value: Any) -> Any:
    """Plain JSON values: numpy numbers become Python numbers and NaN becomes null."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(_clean(payload), indent=1, ensure_ascii=False, allow_nan=False).encode()


def _verdict(low: float, high: float, matches: int) -> str:
    if matches < MIN_MATCHES_FOR_VERDICT or not (math.isfinite(low) and math.isfinite(high)):
        return "too_early"
    if high < 0:
        return "better"
    if low > 0:
        return "worse"
    return "not_distinguishable"


def live_summary(scoring: Scoring, matches: pd.DataFrame, *, now: datetime) -> dict[str, Any]:
    """The scoreboard: per-model scores, the primary model against the reference, the trend."""
    scored = scoring.scored
    payload: dict[str, Any] = {
        "generated_at": iso(now),
        "primary_model": PRIMARY_MODEL,
        "reference_model": REFERENCE_MODEL,
        "min_matches_for_verdict": MIN_MATCHES_FOR_VERDICT,
        "matches_scored": 0,
        "models": {},
        "primary_vs_reference": None,
        "trend": [],
        "recent": [],
    }
    if scored.empty:
        return payload

    table = summarize(scored)
    reference_rps = (
        float(table.loc[REFERENCE_MODEL, "rps"]) if REFERENCE_MODEL in table.index else None
    )
    payload["matches_scored"] = int(scored["match_id"].nunique())
    for name, row in table.iterrows():
        payload["models"][str(name)] = {
            "matches": int(row["matches"]),
            "rps": float(row["rps"]),
            "log_loss": float(row["log_loss"]),
            "brier": float(row["brier"]),
            "accuracy": float(row["accuracy"]),
            "rps_skill_vs_reference": (
                None if reference_rps is None else 1.0 - float(row["rps"]) / reference_rps
            ),
        }

    if PRIMARY_MODEL in table.index and REFERENCE_MODEL in table.index:
        gap = paired_difference(scored, PRIMARY_MODEL, REFERENCE_MODEL)
        payload["primary_vs_reference"] = {
            "matches": int(gap["matches"]),
            "mean_rps_difference": gap["mean"],
            "ci95_low": gap["ci_low"],
            "ci95_high": gap["ci_high"],
            "verdict": _verdict(gap["ci_low"], gap["ci_high"], int(gap["matches"])),
        }

    payload["trend"] = _trend(scored)
    payload["recent"] = _recent(scored, matches)
    return payload


def _trend(scored: pd.DataFrame) -> list[dict[str, Any]]:
    """Running mean RPS and skill after each matchday, in the order the matches were played."""
    wide = scored.pivot(index="match_id", columns="model", values="rps")
    order = (
        scored.drop_duplicates("match_id")
        .sort_values(["kickoff_utc", "match_id"])[["match_id", "season", "matchday"]]
        .reset_index(drop=True)
    )
    wide = wide.loc[order["match_id"]].reset_index(drop=True)
    running = wide.expanding().mean()
    last_of_round = ~order.duplicated(["season", "matchday"], keep="last")
    points = []
    for index in order.index[last_of_round]:
        rps = {str(m): float(v) for m, v in running.loc[index].items()}
        reference = rps.get(REFERENCE_MODEL)
        points.append(
            {
                "season": int(order.at[index, "season"]),
                "matchday": int(order.at[index, "matchday"]),
                "matches": int(index) + 1,
                "rps": rps,
                "skill": ({m: 1.0 - v / reference for m, v in rps.items()} if reference else {}),
            }
        )
    return points


def _recent(scored: pd.DataFrame, matches: pd.DataFrame) -> list[dict[str, Any]]:
    primary = scored[scored["model"] == PRIMARY_MODEL]
    latest = primary.sort_values(["kickoff_utc", "match_id"], ascending=False).head(RECENT_LIMIT)
    info = matches.set_index("match_id")
    rows = []
    for _, row in latest.iterrows():
        match = info.loc[int(row["match_id"])]
        rows.append(
            {
                "match_id": int(row["match_id"]),
                "matchday": int(row["matchday"]),
                "kickoff_utc": iso(row["kickoff_utc"]),
                "home_team": str(match["home_team"]),
                "away_team": str(match["away_team"]),
                "home_goals": int(match["home_goals"]),
                "away_goals": int(match["away_goals"]),
                "outcome": OUTCOME_LABELS[int(row["outcome"])],
                "p_home": float(row["p_home"]),
                "p_draw": float(row["p_draw"]),
                "p_away": float(row["p_away"]),
                "rps": float(row["rps"]),
                "correct": bool(row["correct"]),
            }
        )
    return rows


def upcoming_fixtures(
    matches: pd.DataFrame,
    features: pd.DataFrame,
    records: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """The next fixtures with their frozen forecasts, or the time each one will be frozen."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    ahead = matches[(matches["kickoff_utc"] > now_ts) & ~matches["is_finished"]]
    ahead = ahead.sort_values(["kickoff_utc", "match_id"])
    rounds = list(zip(ahead["season"], ahead["matchday"], strict=True))
    next_two = list(dict.fromkeys(rounds))[:2]
    ahead = ahead[[round_ in next_two for round_ in rounds]].head(UPCOMING_LIMIT)

    frozen = {int(r["match_id"]): r for r in records}
    as_of = features.set_index("match_id")["as_of_utc"]
    fixtures = []
    for _, match in ahead.iterrows():
        match_id = int(match["match_id"])
        item: dict[str, Any] = {
            "match_id": match_id,
            "matchday": int(match["matchday"]),
            "kickoff_utc": iso(match["kickoff_utc"]),
            "home_team": str(match["home_team"]),
            "away_team": str(match["away_team"]),
        }
        record = frozen.get(match_id)
        if record is not None:
            item["status"] = "frozen"
            item["frozen_at_utc"] = record["frozen_at_utc"]
            item["models"] = record["models"]
        else:
            item["status"] = "scheduled"
            item["freezes_at_utc"] = iso(as_of.loc[match_id]) if match_id in as_of.index else None
        fixtures.append(item)
    return {
        "generated_at": iso(now),
        "primary_model": PRIMARY_MODEL,
        "outcome_order": list(OUTCOME_LABELS),
        "fixtures": fixtures,
    }
