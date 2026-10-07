"""Walk-forward evaluation: replay history and score what each model would have said at the time.

Before every matchday the models are refitted on matches whose results were known when that
matchday's predictions would have been frozen (`first kickoff of the matchday - lead`). They then
predict the matchday. No model ever sees a result from its own matchday or later, and the Elo
ratings and form features behind each row are point-in-time too (see `features.py`).

The model settings and Elo constants are fixed defaults that were not tuned on Bundesliga results.
Tuning them against these scores would make the scores optimistic.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from spieltag.contracts import validate_predictions
from spieltag.features import DEFAULT_LEAD, OUTCOME_LABELS, RESULT_DELAY
from spieltag.metrics import (
    PROBABILITY_COLUMNS,
    calibration_table,
    paired_difference,
    score_predictions,
    summarize,
)
from spieltag.models import Model
from spieltag.store import Store

PREDICTION_COLUMNS = [
    "match_id",
    "season",
    "matchday",
    "kickoff_utc",
    "model",
    *PROBABILITY_COLUMNS,
    "outcome",
    "train_matches",
    "trained_until",
]


def round_starts(features: pd.DataFrame) -> pd.Series:
    """First kickoff of every matchday, indexed by (season, matchday)."""
    return features.groupby(["season", "matchday"])["kickoff_utc"].min()


def training_window(features: pd.DataFrame, frozen_at: pd.Timestamp) -> pd.DataFrame:
    """The finished matches whose result was already known at `frozen_at`.

    The backtest and the live pipeline both cut their training data with this one function, so
    what the live models learn from is, by construction, what the backtest said they would.
    """
    finished = features[features["is_finished"]]
    return finished[finished["kickoff_utc"] + RESULT_DELAY <= frozen_at]


def walk_forward(
    features: pd.DataFrame,
    factories: dict[str, Callable[[], Model]],
    *,
    first_test_season: int,
    lead: timedelta = DEFAULT_LEAD,
    min_train_matches: int = 600,
) -> pd.DataFrame:
    """Predict every finished match from `first_test_season` on, matchday by matchday.

    Returns one row per match and model. Matchdays with fewer than `min_train_matches` finished
    training matches are skipped.
    """
    finished = features[features["is_finished"]]
    starts = round_starts(features)

    pieces: list[pd.DataFrame] = []
    test_pool = finished[finished["season"] >= first_test_season]
    for (season, matchday), games in test_pool.groupby(["season", "matchday"], sort=True):
        frozen_at = starts[(season, matchday)] - lead
        train = training_window(features, frozen_at)
        if len(train) < min_train_matches:
            continue
        for name, factory in factories.items():
            model = factory().fit(train)
            probabilities = model.predict_proba(games)
            piece = games[["match_id", "season", "matchday", "kickoff_utc"]].copy()
            piece["model"] = name
            piece[PROBABILITY_COLUMNS] = probabilities
            piece["outcome"] = games["outcome"].astype("int64").to_numpy()
            piece["train_matches"] = len(train)
            piece["trained_until"] = frozen_at
            pieces.append(piece)

    if not pieces:
        return pd.DataFrame(columns=PREDICTION_COLUMNS)
    predictions = pd.concat(pieces, ignore_index=True)[PREDICTION_COLUMNS]
    for column in ("match_id", "season", "matchday", "train_matches"):
        predictions[column] = predictions[column].astype("int64")
    return validate_predictions(predictions)


def _number(value: Any, digits: int = 5) -> float:
    return round(float(value), digits)


def build_report(
    predictions: pd.DataFrame,
    *,
    league: str,
    seasons_loaded: list[int],
    first_test_season: int,
    lead: timedelta,
    min_train_matches: int,
    reference: str = "climatology",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything a reader needs to judge the models, as plain JSON-friendly data."""
    scored = score_predictions(predictions)
    table = summarize(scored)
    best = str(table.index[0])
    reference_rps = float(table.loc[reference, "rps"]) if reference in table.index else None

    models = {}
    for name, row in table.iterrows():
        entry = {
            "matches": int(row["matches"]),
            "rps": _number(row["rps"]),
            "log_loss": _number(row["log_loss"]),
            "brier": _number(row["brier"]),
            "accuracy": _number(row["accuracy"]),
        }
        if reference_rps is not None:
            entry["rps_skill_vs_reference"] = _number(1.0 - row["rps"] / reference_rps)
        models[str(name)] = entry

    by_season = scored.groupby(["season", "model"])["rps"].mean().unstack("model")
    per_season = {
        str(season): {str(m): _number(v) for m, v in row.items()}
        for season, row in by_season.iterrows()
    }

    paired = {}
    for other in table.index:
        if other != best:
            result = paired_difference(scored, best, str(other))
            paired[f"{best}_minus_{other}"] = {
                "matches": int(result["matches"]),
                "mean_rps_difference": _number(result["mean"]),
                "ci95_low": _number(result["ci_low"]),
                "ci95_high": _number(result["ci_high"]),
            }

    calibration = [
        {
            "band": row["band"],
            "matches": int(row["matches"]),
            "forecast": _number(row["forecast"], 4),
            "observed": _number(row["observed"], 4),
        }
        for row in calibration_table(scored, best).to_dict("records")
    ]

    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "league": league,
        "protocol": {
            "validation": "walk-forward, models refitted before every matchday",
            "prediction_lead_hours": lead.total_seconds() / 3600,
            "result_known_after_kickoff_hours": RESULT_DELAY.total_seconds() / 3600,
            "min_train_matches": min_train_matches,
            "outcome_order": list(OUTCOME_LABELS),
            "headline_metric": "ranked probability score (lower is better)",
            "reference_model": reference,
            "tuning": "none: model settings and Elo constants are fixed defaults",
        },
        "data": {
            "seasons_loaded": seasons_loaded,
            "first_test_season": first_test_season,
            "evaluated_matches": int(scored["match_id"].nunique()),
        },
        "best_model": best,
        "models": models,
        "per_season_rps": per_season,
        "paired_with_best": paired,
        "calibration_of_best_home_win": calibration,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """A short, readable version of the report for the README and the terminal."""
    protocol, data = report["protocol"], report["data"]
    lines = [
        f"# Walk-forward evaluation: {report['league']}",
        "",
        f"{data['evaluated_matches']} finished matches from season {data['first_test_season']} on. "
        f"Models are refitted before every matchday on results known "
        f"{protocol['prediction_lead_hours']:g} hours before its first kickoff. "
        "Lower is better for every score.",
        "",
        "| Model | Matches | RPS | Log loss | Brier | Accuracy | RPS skill vs climatology |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, m in report["models"].items():
        skill = m.get("rps_skill_vs_reference")
        skill_text = (
            "-" if skill is None or name == protocol["reference_model"] else f"{skill:+.1%}"
        )
        lines.append(
            f"| {name} | {m['matches']} | {m['rps']:.4f} | {m['log_loss']:.4f} | "
            f"{m['brier']:.4f} | {m['accuracy']:.1%} | {skill_text} |"
        )

    names = list(report["models"])
    lines += ["", "## Ranked probability score by season", ""]
    lines.append("| Season | " + " | ".join(names) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in names) + " |")
    for season, values in report["per_season_rps"].items():
        cells = " | ".join(f"{values[n]:.4f}" if n in values else "-" for n in names)
        lines.append(f"| {season} | {cells} |")

    lines += ["", f"## Is the best model ({report['best_model']}) really better?", ""]
    for key, p in report["paired_with_best"].items():
        other = key.split("_minus_")[1]
        verdict = "clearly better" if p["ci95_high"] < 0 else "not distinguishable from"
        lines.append(
            f"- vs {other}: RPS difference {p['mean_rps_difference']:+.4f} "
            f"(95% interval {p['ci95_low']:+.4f} to {p['ci95_high']:+.4f}), "
            f"{verdict} on these matches."
        )

    lines += [
        "",
        f"## Calibration of {report['best_model']}: forecast vs observed home wins",
        "",
        "| Forecast band | Matches | Mean forecast | Observed |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in report["calibration_of_best_home_win"]:
        lines.append(
            f"| {row['band']} | {row['matches']} | {row['forecast']:.3f} | {row['observed']:.3f} |"
        )
    lines += [
        "",
        "Model settings and Elo constants were fixed before running this and never tuned on these "
        "results.",
        "",
    ]
    return "\n".join(lines)


def save_evaluation(
    store: Store, league: str, predictions: pd.DataFrame, report: dict[str, Any]
) -> dict[str, str]:
    """Write the predictions, the JSON report and the Markdown report. Returns their locations."""
    buffer = io.BytesIO()
    predictions.to_parquet(buffer, index=False)
    prefix = f"evaluation/{league}"
    return {
        "predictions": store.put_bytes(f"{prefix}/predictions.parquet", buffer.getvalue()),
        "report_json": store.put_bytes(
            f"{prefix}/report.json", json.dumps(report, indent=2).encode("utf-8")
        ),
        "report_md": store.put_bytes(
            f"{prefix}/report.md", render_markdown(report).encode("utf-8")
        ),
    }
