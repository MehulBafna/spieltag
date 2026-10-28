"""Estimating the in-play parameters from past goal times, and checking them on later seasons.

Two things are estimated, both by counting, with no black box:

* the share of goals that fall in the first half, and
* how a team's scoring rate changes when it is trailing or leading, relative to a level game.

For a state, the factor is `goals scored in that state / goals the pre-match rates expected there`,
divided by the same ratio for level games. Goals are counted in 5-minute slices and the state is
the score at the start of the slice. The interval comes from the Poisson counts.

The factors are then shrunk towards 1 in proportion to how few goals they rest on, and they are
only worth using if they beat the neutral model on seasons they were not fitted on.
"""

from __future__ import annotations

import io
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from spieltag.inplay import (
    CHECKPOINTS,
    FIRST_HALF_SHARE,
    InplayParams,
    StateFactors,
    _field,
    fit_rates,
    goal_events,
    minute_weights,
    outcome_probabilities,
)
from spieltag.metrics import PROBABILITY_COLUMNS, rps
from spieltag.models import PRIMARY_MODEL
from spieltag.store import Store

SLICE_MINUTES = 5
SLICES = 18
SHRINK_GOALS = 50  # a factor resting on this many goals is pulled halfway towards 1


@dataclass
class MatchGoals:
    match_id: int
    season: int
    sealed: tuple[float, float, float]
    lam_home: float
    lam_away: float
    events: list[tuple[int, int, int]]  # (minute, home score, away score) after each goal
    final: tuple[int, int]

    @property
    def outcome(self) -> int:
        diff = self.final[0] - self.final[1]
        return 0 if diff > 0 else (1 if diff == 0 else 2)

    def score_at(self, minute: int) -> tuple[int, int]:
        """Score after all goals up to and including `minute`."""
        score = (0, 0)
        for goal_minute, home, away in self.events:
            if goal_minute <= minute:
                score = (home, away)
        return score


def slice_index(minute: int) -> int:
    """Which 5-minute slice a goal belongs to. Minute 1 to 5 is slice 0, and so on."""
    return 0 if minute <= 0 else min((minute - 1) // SLICE_MINUTES, SLICES - 1)


# ---------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------


def latest_raw(store: Store, league: str, season: int) -> list[dict[str, Any]] | None:
    keys = store.list_keys(f"raw/openligadb/{league}/{season}/")
    return json.loads(store.get_bytes(keys[-1])) if keys else None


def load_goal_matches(
    store: Store, league: str, predictions: pd.DataFrame
) -> tuple[list[MatchGoals], dict[str, int]]:
    """Finished matches with sealed-style probabilities and a complete goal list.

    A match is skipped if its goals are not all listed (the number of goals must equal the final
    score), because a partial timeline would bias every count. The skipped count is returned.
    """
    sealed = predictions[predictions["model"] == PRIMARY_MODEL].set_index("match_id")
    matches: list[MatchGoals] = []
    skipped = {"no_prediction": 0, "goals_incomplete": 0, "not_finished": 0}
    for season in sorted(int(s) for s in sealed["season"].unique()):
        raw = latest_raw(store, league, season)
        if raw is None:
            continue
        for raw_match in raw:
            match_id = int(_field(raw_match, "matchID"))
            if not _field(raw_match, "matchIsFinished", False):
                skipped["not_finished"] += 1
                continue
            if match_id not in sealed.index:
                skipped["no_prediction"] += 1
                continue
            final = None
            for result in _field(raw_match, "matchResults", None) or []:
                if _field(result, "resultTypeID") == 2:
                    final = (int(_field(result, "pointsTeam1")), int(_field(result, "pointsTeam2")))
            events = goal_events(raw_match)
            if final is None or len(events) != sum(final) or (events and events[-1][1:] != final):
                skipped["goals_incomplete"] += 1
                continue
            row = sealed.loc[match_id]
            probabilities = tuple(float(row[c]) for c in PROBABILITY_COLUMNS)
            lam_home, lam_away, _ = fit_rates(*probabilities)
            matches.append(
                MatchGoals(match_id, season, probabilities, lam_home, lam_away, events, final)
            )
    return matches, skipped


# ---------------------------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------------------------


def estimate_first_half_share(matches: Sequence[MatchGoals]) -> tuple[float, int]:
    """Share of goals in minutes 1 to 45, and the number of goals it rests on."""
    first = total = 0
    for match in matches:
        for minute, _, _ in match.events:
            total += 1
            first += minute <= 45
    return (first / total if total else FIRST_HALF_SHARE), total


def _scoring_side(events: list[tuple[int, int, int]], index: int) -> int:
    """0 if the home team scored goal `index`, 1 if the away team did."""
    _, home, away = events[index]
    previous_home, previous_away = events[index - 1][1:] if index else (0, 0)
    return 0 if home > previous_home else 1


def estimate_state_factors(
    matches: Sequence[MatchGoals], first_half_share: float
) -> dict[str, dict[str, float]]:
    """Per state: goals, expected goals, the factor relative to level play, and its 95% interval."""
    weights = minute_weights(first_half_share)
    mass = weights.reshape(SLICES, SLICE_MINUTES).sum(axis=1)
    goals = dict.fromkeys(("trail2", "trail1", "level", "lead1", "lead2"), 0.0)
    expected = dict.fromkeys(goals, 0.0)

    def state_of(diff: int) -> str:
        return (
            "trail2"
            if diff <= -2
            else "trail1"
            if diff == -1
            else "level"
            if diff == 0
            else "lead1"
            if diff == 1
            else "lead2"
        )

    for match in matches:
        by_slice: list[list[int]] = [[] for _ in range(SLICES)]
        for i, (minute, _, _) in enumerate(match.events):
            by_slice[slice_index(minute)].append(i)
        score = (0, 0)
        for s in range(SLICES):
            home_state = state_of(score[0] - score[1])
            away_state = state_of(score[1] - score[0])
            expected[home_state] += match.lam_home * mass[s]
            expected[away_state] += match.lam_away * mass[s]
            for i in by_slice[s]:
                side = _scoring_side(match.events, i)
                goals[home_state if side == 0 else away_state] += 1
            if by_slice[s]:
                score = match.events[by_slice[s][-1]][1:]

    level_ratio = goals["level"] / expected["level"] if expected["level"] else float("nan")
    table: dict[str, dict[str, float]] = {}
    for name in goals:
        raw_ratio = goals[name] / expected[name] if expected[name] else float("nan")
        relative = raw_ratio / level_ratio if level_ratio else float("nan")
        if goals[name] > 0 and goals["level"] > 0:
            se = math.sqrt(1 / goals[name] + 1 / goals["level"])
            low, high = relative * math.exp(-1.96 * se), relative * math.exp(1.96 * se)
        else:
            low = high = float("nan")
        table[name] = {
            "goals": goals[name],
            "expected": expected[name],
            "relative": 1.0 if name == "level" else relative,
            "low": 1.0 if name == "level" else low,
            "high": 1.0 if name == "level" else high,
        }
    table["level"]["goal_scale"] = level_ratio
    return table


def shrink(table: dict[str, dict[str, float]]) -> StateFactors:
    """Pull each factor towards 1 in proportion to how few goals it rests on."""
    values = {}
    for name in ("trail2", "trail1", "lead1", "lead2"):
        entry = table[name]
        if not math.isfinite(entry["relative"]) or entry["relative"] <= 0:
            values[name] = 1.0
            continue
        weight = entry["goals"] / (entry["goals"] + SHRINK_GOALS)
        values[name] = math.exp(weight * math.log(entry["relative"]))
    return StateFactors(**values)


def fit_params(matches: Sequence[MatchGoals], source: str) -> tuple[InplayParams, dict[str, Any]]:
    share, share_goals = estimate_first_half_share(matches)
    table = estimate_state_factors(matches, share)
    params = InplayParams(share, shrink(table), source)
    return params, {"first_half_goals": share_goals, "states": table}


# ---------------------------------------------------------------------------------------------
# Held-out check
# ---------------------------------------------------------------------------------------------


def evaluate_variants(
    matches: Sequence[MatchGoals], variants: dict[str, InplayParams]
) -> pd.DataFrame:
    """RPS per match, checkpoint and variant. The sealed forecast is always included."""
    rows = []
    for match in matches:
        outcome = np.array([match.outcome])
        for minute in CHECKPOINTS:
            score = match.score_at(minute)
            base = {"match_id": match.match_id, "season": match.season, "minute": minute}
            rows.append(
                {
                    **base,
                    "variant": "sealed",
                    "rps": float(rps(np.array([match.sealed]), outcome)[0]),
                }
            )
            for name, params in variants.items():
                probabilities, _, _ = outcome_probabilities(
                    match.lam_home, match.lam_away, minute, score, params
                )
                rows.append(
                    {
                        **base,
                        "variant": name,
                        "rps": float(rps(probabilities[None, :], outcome)[0]),
                    }
                )
    return pd.DataFrame(rows)


def summarize_variants(scores: pd.DataFrame) -> pd.DataFrame:
    """Mean RPS by checkpoint minute and variant."""
    return scores.pivot_table(index="minute", columns="variant", values="rps", aggfunc="mean")


def paired_over_matches(scores: pd.DataFrame, a: str, b: str) -> dict[str, float]:
    """Mean RPS of `a` minus `b` over matches (checkpoints averaged within each match)."""
    per_match = scores.groupby(["match_id", "variant"])["rps"].mean().unstack("variant")
    gap = (per_match[a] - per_match[b]).to_numpy(dtype=float)
    mean = float(gap.mean())
    half = 1.96 * float(gap.std(ddof=1)) / math.sqrt(len(gap)) if len(gap) > 1 else float("nan")
    return {"matches": len(gap), "mean": mean, "low": mean - half, "high": mean + half}


def render_report(
    table: dict[str, dict[str, float]],
    share: float,
    share_goals: int,
    held_out: pd.DataFrame | None,
    pairs: dict[str, dict[str, float]] | None,
    coverage: dict[str, int],
    used: int,
) -> str:
    lines = [
        "# In-play parameters",
        "",
        f"{used} finished matches with complete goal lists. Skipped: {coverage}.",
        "",
        f"First-half share of goals: {share:.3f} (from {share_goals} goals). The default is "
        f"{FIRST_HALF_SHARE:.2f}.",
        "",
        "| State | Goals | Expected goals | Factor vs level | 95% interval |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name in ("trail2", "trail1", "level", "lead1", "lead2"):
        e = table[name]
        interval = "-" if name == "level" else f"{e['low']:.2f} to {e['high']:.2f}"
        lines.append(
            f"| {name} | {e['goals']:.0f} | {e['expected']:.1f} | "
            f"{e['relative']:.2f} | {interval} |"
        )
    scale = table["level"].get("goal_scale")
    if scale is not None and math.isfinite(scale):
        lines += [
            "",
            f"In level games the pre-match rates predicted {1 / scale:.2f} times the goals that "
            "were scored. Far from 1 would mean the rates are biased.",
        ]
    if held_out is not None and pairs is not None:
        lines += ["", "## Held-out seasons: mean ranked probability score (lower is better)", ""]
        columns = list(held_out.columns)
        lines.append("| Minute | " + " | ".join(columns) + " |")
        lines.append("| ---: | " + " | ".join("---:" for _ in columns) + " |")
        for minute, row in held_out.iterrows():
            lines.append(f"| {minute} | " + " | ".join(f"{row[c]:.4f}" for c in columns) + " |")
        lines.append("")
        for label, p in pairs.items():
            verdict = (
                "better" if p["high"] < 0 else "worse" if p["low"] > 0 else "not distinguishable"
            )
            lines.append(
                f"- {label}: {p['mean']:+.4f} (95% interval {p['low']:+.4f} to {p['high']:+.4f}, "
                f"{p['matches']} matches), {verdict}."
            )
    return "\n".join(lines) + "\n"


def predictions_from_store(store: Store, league: str) -> pd.DataFrame:
    data = store.get_bytes(f"evaluation/{league}/predictions.parquet")
    return pd.read_parquet(io.BytesIO(data))
