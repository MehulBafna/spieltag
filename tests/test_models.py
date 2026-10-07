import numpy as np
import pandas as pd
import pytest

from spieltag.features import MODEL_FEATURES, build_features
from spieltag.metrics import rps
from spieltag.models import (
    ClimatologyModel,
    MultinomialLogitModel,
    OrderedLogitModel,
    default_models,
)


@pytest.fixture(scope="module")
def table(league_matches):
    features = build_features(league_matches)
    return features[features["is_finished"]]


def test_climatology_returns_the_training_frequencies_to_everyone(table):
    model = ClimatologyModel().fit(table)
    probabilities = model.predict_proba(table.head(5))
    assert probabilities.shape == (5, 3)
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    assert (probabilities == probabilities[0]).all()
    frequencies = table["outcome"].value_counts(normalize=True).sort_index().to_numpy()
    assert np.allclose(probabilities[0], frequencies, atol=0.005)


def test_ordered_logit_gives_valid_probabilities(table):
    probabilities = OrderedLogitModel().fit(table).predict_proba(table)
    assert probabilities.shape == (len(table), 3)
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    assert (probabilities > 0).all() and (probabilities < 1).all()


def test_a_bigger_rating_gap_always_means_a_likelier_home_win(table):
    model = OrderedLogitModel().fit(table)
    grid = pd.DataFrame({"elo_diff": np.linspace(-300, 300, 41)})
    probabilities = model.predict_proba(grid)
    assert (np.diff(probabilities[:, 0]) > 0).all()  # home win gets likelier
    assert (np.diff(probabilities[:, 2]) < 0).all()  # away win gets less likely
    assert probabilities[:, 1].max() < 0.5  # a draw never becomes the favourite


def test_ordered_logit_beats_climatology_on_data_with_real_signal(table):
    train, test = table.iloc[:900], table.iloc[900:]
    outcome = test["outcome"].to_numpy(dtype=int)
    baseline = rps(ClimatologyModel().fit(train).predict_proba(test), outcome).mean()
    model = rps(OrderedLogitModel().fit(train).predict_proba(test), outcome).mean()
    assert model < baseline


def test_multinomial_logit_gives_valid_probabilities(table):
    probabilities = MultinomialLogitModel().fit(table).predict_proba(table)
    assert probabilities.shape == (len(table), 3)
    assert np.allclose(probabilities.sum(axis=1), 1.0)


def test_multinomial_logit_needs_all_three_outcomes(table):
    only_home_wins = table[table["outcome"] == 0]
    with pytest.raises(ValueError, match="draws"):
        MultinomialLogitModel().fit(only_home_wins)


def test_models_refuse_missing_features(table):
    broken = table.copy()
    broken.loc[broken.index[0], "elo_diff"] = np.nan
    with pytest.raises(ValueError, match="missing"):
        OrderedLogitModel().fit(broken)


def test_models_refuse_training_rows_without_an_outcome(table):
    unplayed = table.copy()
    unplayed["outcome"] = pd.array([pd.NA] * len(unplayed), dtype="Int64")
    with pytest.raises(ValueError, match="outcome"):
        ClimatologyModel().fit(unplayed)


def test_default_models_are_fresh_and_named():
    factories = default_models()
    assert list(factories) == ["climatology", "elo_ordered_logit", "feature_logit"]
    assert factories["feature_logit"]() is not factories["feature_logit"]()
    for name, factory in factories.items():
        assert factory().name == name


def test_the_feature_model_uses_the_documented_columns():
    assert MultinomialLogitModel().features == tuple(MODEL_FEATURES)
