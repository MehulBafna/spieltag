import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from spieltag.contracts import ContractError, validate_predictions
from spieltag.evaluate import build_report, render_markdown, walk_forward
from spieltag.features import DEFAULT_LEAD, RESULT_DELAY, build_features
from spieltag.metrics import score_predictions, summarize
from spieltag.models import ClimatologyModel, default_models

FIRST_TEST = 2018
MIN_TRAIN = 500


@pytest.fixture(scope="module")
def signal(league_matches):
    features = build_features(league_matches)
    predictions = walk_forward(
        features, default_models(), first_test_season=FIRST_TEST, min_train_matches=MIN_TRAIN
    )
    return features, predictions


@pytest.fixture(scope="module")
def noise(noise_matches):
    features = build_features(noise_matches)
    return walk_forward(
        features, default_models(), first_test_season=FIRST_TEST, min_train_matches=MIN_TRAIN
    )


def test_every_finished_match_from_the_first_test_season_is_predicted_by_every_model(signal):
    features, predictions = signal
    expected = features[features["is_finished"] & (features["season"] >= FIRST_TEST)]
    assert len(predictions) == 3 * len(expected)
    assert set(predictions["model"]) == {"climatology", "elo_ordered_logit", "feature_logit"}
    assert predictions.groupby("model")["match_id"].nunique().eq(len(expected)).all()


def test_predictions_follow_the_contract(signal):
    _, predictions = signal
    validate_predictions(predictions)


def test_models_are_trained_only_on_results_known_before_the_prediction_was_frozen(signal):
    """A spy model records what it is trained on and what it is asked to predict."""
    features, _ = signal
    fits = []

    class Spy(ClimatologyModel):
        def fit(self, frame):
            self.train_ids = set(frame["match_id"])
            self.train_known = frame["kickoff_utc"] + RESULT_DELAY
            self.train_seasons = set(frame["season"])
            return super().fit(frame)

        def predict_proba(self, frame):
            fits.append((self, frame))
            return super().predict_proba(frame)

    walk_forward(features, {"spy": Spy}, first_test_season=FIRST_TEST, min_train_matches=MIN_TRAIN)

    assert len(fits) > 50
    for model, games in fits:
        frozen_at = games["kickoff_utc"].min() - DEFAULT_LEAD
        assert (model.train_known <= frozen_at).all()  # every training result was already known
        assert not model.train_ids & set(games["match_id"])  # never trained on what it predicts
        assert max(model.train_seasons) <= games["season"].max()


def test_the_first_matchdays_are_skipped_until_there_is_enough_history(league_matches):
    features = build_features(league_matches)
    predictions = walk_forward(
        features, {"c": ClimatologyModel}, first_test_season=2016, min_train_matches=MIN_TRAIN
    )
    assert predictions["train_matches"].min() >= MIN_TRAIN
    assert predictions["season"].min() == 2017  # 2016 had nothing to learn from


def test_nothing_is_predicted_without_enough_history(league_matches):
    features = build_features(league_matches)
    predictions = walk_forward(
        features, {"c": ClimatologyModel}, first_test_season=2016, min_train_matches=10_000
    )
    assert predictions.empty


def test_the_models_find_real_signal(signal):
    _, predictions = signal
    table = summarize(score_predictions(predictions))
    reference = table.loc["climatology", "rps"]
    assert table.loc["elo_ordered_logit", "rps"] < reference - 0.01
    assert table.loc["feature_logit", "rps"] < reference - 0.01


def test_on_data_with_no_signal_no_model_beats_the_league_averages(noise):
    """The leakage canary: if the future leaked in, models would 'find' skill here."""
    table = summarize(score_predictions(noise))
    reference = table.loc["climatology", "rps"]
    assert table.loc["elo_ordered_logit", "rps"] > reference - 0.003
    assert table.loc["feature_logit", "rps"] > reference - 0.003


def test_the_report_is_complete_and_json_friendly(signal):
    _, predictions = signal
    report = build_report(
        predictions,
        league="sim",
        seasons_loaded=list(range(2016, 2021)),
        first_test_season=FIRST_TEST,
        lead=timedelta(hours=24),
        min_train_matches=MIN_TRAIN,
    )
    json.dumps(report)  # raises if anything is not plain data
    assert report["best_model"] in report["models"]
    assert report["models"]["climatology"]["matches"] == report["data"]["evaluated_matches"]
    assert report["models"]["elo_ordered_logit"]["rps_skill_vs_reference"] > 0
    assert set(report["per_season_rps"]) == {"2018", "2019", "2020"}
    assert len(report["paired_with_best"]) == 2
    assert report["protocol"]["tuning"].startswith("none")

    markdown = render_markdown(report)
    for name in report["models"]:
        assert name in markdown
    assert "Calibration" in markdown and "RPS" in markdown


def test_probabilities_that_do_not_sum_to_one_break_the_contract(signal):
    _, predictions = signal
    broken = predictions.copy()
    broken.loc[broken.index[0], "p_home"] = 0.99
    with pytest.raises(ContractError, match="sum"):
        validate_predictions(broken)


def test_a_duplicated_prediction_breaks_the_contract(signal):
    _, predictions = signal
    with pytest.raises(ContractError):
        validate_predictions(pd.concat([predictions, predictions.head(1)], ignore_index=True))


def test_results_are_deterministic(league_matches, signal):
    _, first = signal
    again = walk_forward(
        build_features(league_matches),
        default_models(),
        first_test_season=FIRST_TEST,
        min_train_matches=MIN_TRAIN,
    )
    assert np.allclose(first[["p_home", "p_draw", "p_away"]], again[["p_home", "p_draw", "p_away"]])
