"""Scores for probability forecasts of home win, draw and away win.

Probabilities are always ordered (home, draw, away) and outcomes are coded 0, 1, 2 the same way.
The headline score is the ranked probability score (RPS). It treats the outcomes as ordered, so a
forecast that expected a home win is punished less for a draw than for an away win. Lower is better.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PROBABILITY_COLUMNS = ["p_home", "p_draw", "p_away"]
_EPS = 1e-12


def _one_hot(outcome: np.ndarray) -> np.ndarray:
    return np.eye(3)[np.asarray(outcome, dtype=int)]


def rps(probabilities: np.ndarray, outcome: np.ndarray) -> np.ndarray:
    """Ranked probability score per match: half the squared gaps between the cumulative curves."""
    cumulative_forecast = np.cumsum(probabilities, axis=1)[:, :2]
    cumulative_outcome = np.cumsum(_one_hot(outcome), axis=1)[:, :2]
    return 0.5 * ((cumulative_forecast - cumulative_outcome) ** 2).sum(axis=1)


def log_loss(probabilities: np.ndarray, outcome: np.ndarray) -> np.ndarray:
    """Minus the log of the probability given to what actually happened."""
    chosen = probabilities[np.arange(len(outcome)), np.asarray(outcome, dtype=int)]
    return -np.log(np.clip(chosen, _EPS, None))


def brier(probabilities: np.ndarray, outcome: np.ndarray) -> np.ndarray:
    """Squared distance between the forecast and the one-hot outcome, summed over the three."""
    return ((probabilities - _one_hot(outcome)) ** 2).sum(axis=1)


def score_predictions(predictions: pd.DataFrame) -> pd.DataFrame:
    """Add per-match `rps`, `log_loss`, `brier` and `correct` columns to a predictions table."""
    probabilities = predictions[PROBABILITY_COLUMNS].to_numpy(dtype=float)
    outcome = predictions["outcome"].to_numpy(dtype=int)
    scored = predictions.copy()
    scored["rps"] = rps(probabilities, outcome)
    scored["log_loss"] = log_loss(probabilities, outcome)
    scored["brier"] = brier(probabilities, outcome)
    scored["correct"] = probabilities.argmax(axis=1) == outcome
    return scored


def summarize(scored: pd.DataFrame) -> pd.DataFrame:
    """One row per model: matches and the mean of each score."""
    grouped = scored.groupby("model")
    table = grouped.agg(
        matches=("rps", "size"),
        rps=("rps", "mean"),
        log_loss=("log_loss", "mean"),
        brier=("brier", "mean"),
        accuracy=("correct", "mean"),
    )
    return table.sort_values("rps")


def paired_difference(
    scored: pd.DataFrame, model_a: str, model_b: str, metric: str = "rps"
) -> dict[str, float]:
    """Mean of (model_a - model_b) over the same matches, with a 95% normal-approximation interval.

    Negative means model_a scored better (lower). If the interval contains zero, the data cannot
    tell the two models apart.
    """
    wide = scored.pivot(index="match_id", columns="model", values=metric).dropna(
        subset=[model_a, model_b]
    )
    gap = (wide[model_a] - wide[model_b]).to_numpy(dtype=float)
    mean = float(gap.mean())
    if len(gap) < 2:
        return {"matches": len(gap), "mean": mean, "ci_low": float("nan"), "ci_high": float("nan")}
    half_width = 1.96 * float(gap.std(ddof=1)) / np.sqrt(len(gap))
    return {
        "matches": len(gap),
        "mean": mean,
        "ci_low": mean - half_width,
        "ci_high": mean + half_width,
    }


def calibration_table(scored: pd.DataFrame, model: str, bins: int = 10) -> pd.DataFrame:
    """For one model: how often home wins happened, per band of forecast home-win probability."""
    own = scored[scored["model"] == model]
    index = np.clip((own["p_home"].to_numpy() * bins).astype(int), 0, bins - 1)
    table = (
        own.assign(band_index=index, home_win=(own["outcome"] == 0).astype(float))
        .groupby("band_index")
        .agg(
            matches=("home_win", "size"), forecast=("p_home", "mean"), observed=("home_win", "mean")
        )
        .reset_index()
    )
    table["band"] = [f"{i / bins:.1f}-{(i + 1) / bins:.1f}" for i in table["band_index"]]
    return table[["band", "matches", "forecast", "observed"]]
