"""The forecasting models. Each one is fitted on a table of past matches with features and an
`outcome` column (0 home win, 1 draw, 2 away win) and returns probabilities ordered
(home, draw, away).

Three models, from simplest to richest, so every step has to earn its place:

1. climatology: how often each result happens in the training data, the same for every match.
2. elo_ordered_logit: only the Elo rating gap, mapped to probabilities with an ordered logit.
3. feature_logit: a regularised multinomial logistic regression on all features.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from spieltag.features import MODEL_FEATURES

_EPS = 1e-12


class Model(Protocol):
    name: str

    def fit(self, frame: pd.DataFrame) -> Model: ...

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        """An array of shape (matches, 3): P(home win), P(draw), P(away win)."""


def _features(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    values = frame[list(columns)].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"features {list(columns)} contain missing or infinite values")
    return values


def _outcomes(frame: pd.DataFrame) -> np.ndarray:
    outcome = frame["outcome"]
    if outcome.isna().any():
        raise ValueError("training rows need a known outcome")
    return outcome.to_numpy(dtype=int)


class ClimatologyModel:
    """Everyone gets the same probabilities: the training frequencies, lightly smoothed."""

    name = "climatology"

    def fit(self, frame: pd.DataFrame) -> ClimatologyModel:
        counts = np.bincount(_outcomes(frame), minlength=3).astype(float)
        self._probabilities = (counts + 1.0) / (counts.sum() + 3.0)
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        return np.tile(self._probabilities, (len(frame), 1))


class OrderedLogitModel:
    """Ordered logit: one latent strength score per match, two cut points.

    With score s = x·beta, P(away win) = sigmoid(c1 - s) and P(away win or draw) = sigmoid(c2 - s),
    where c2 > c1. A higher score always moves probability from away to home, the draw sits in
    between, and the model has only a handful of parameters, so it is hard to overfit.
    """

    def __init__(self, features: Sequence[str] = ("elo_diff",), name: str = "elo_ordered_logit"):
        self.features = tuple(features)
        self.name = name

    def fit(self, frame: pd.DataFrame) -> OrderedLogitModel:
        x, y = _features(frame, self.features), _outcomes(frame)
        self._mean = x.mean(axis=0)
        self._scale = np.where(x.std(axis=0) > 0, x.std(axis=0), 1.0)
        z = (x - self._mean) / self._scale
        k = z.shape[1]
        rows = np.arange(len(y))

        def loss(params: np.ndarray) -> float:
            p = self._probabilities(z, params)
            return float(
                -np.log(np.clip(p[rows, y], _EPS, None)).mean() + 0.5e-3 * params[:k] @ params[:k]
            )

        start = np.concatenate([np.zeros(k), [-0.85, np.log(1.05)]])
        result = minimize(loss, start, method="L-BFGS-B")
        self._params = result.x
        return self

    @staticmethod
    def _probabilities(z: np.ndarray, params: np.ndarray) -> np.ndarray:
        k = z.shape[1]
        score = z @ params[:k]
        cut_1 = params[k]
        cut_2 = cut_1 + np.exp(params[k + 1])
        p_away = expit(cut_1 - score)
        p_away_or_draw = expit(cut_2 - score)
        return np.column_stack([1.0 - p_away_or_draw, p_away_or_draw - p_away, p_away])

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        z = (_features(frame, self.features) - self._mean) / self._scale
        return self._probabilities(z, self._params)


class MultinomialLogitModel:
    """Regularised multinomial logistic regression on standardised features."""

    def __init__(
        self,
        features: Sequence[str] = tuple(MODEL_FEATURES),
        c: float = 0.5,
        name: str = "feature_logit",
    ):
        self.features = tuple(features)
        self.c = c
        self.name = name

    def fit(self, frame: pd.DataFrame) -> MultinomialLogitModel:
        y = _outcomes(frame)
        if set(y) != {0, 1, 2}:
            raise ValueError("the training data must contain home wins, draws and away wins")
        self._pipeline = make_pipeline(
            StandardScaler(), LogisticRegression(C=self.c, max_iter=2000)
        )
        self._pipeline.fit(_features(frame, self.features), y)
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        return self._pipeline.predict_proba(_features(frame, self.features))


def default_models() -> dict[str, Callable[[], Model]]:
    """A fresh, unfitted instance of each model, by name. The names appear in every report."""
    return {
        "climatology": ClimatologyModel,
        "elo_ordered_logit": OrderedLogitModel,
        "feature_logit": MultinomialLogitModel,
    }
