import numpy as np
import pandas as pd
import pytest

from spieltag.metrics import (
    brier,
    calibration_table,
    log_loss,
    paired_difference,
    rps,
    score_predictions,
    summarize,
)


def test_rps_of_a_worked_example():
    # forecast 50/30/20, home win: cumulative (0.5, 0.8) against (1, 1) -> (0.25 + 0.04) / 2
    assert rps(np.array([[0.5, 0.3, 0.2]]), np.array([0]))[0] == pytest.approx(0.145)


def test_rps_is_zero_for_a_perfect_forecast_and_one_for_the_worst_one():
    assert rps(np.array([[0.0, 1.0, 0.0]]), np.array([1]))[0] == 0.0
    assert rps(np.array([[1.0, 0.0, 0.0]]), np.array([2]))[0] == pytest.approx(1.0)


def test_rps_punishes_a_far_miss_more_than_a_near_miss():
    expected_home_win = np.array([[0.7, 0.2, 0.1]])
    near = rps(expected_home_win, np.array([1]))[0]  # a draw
    far = rps(expected_home_win, np.array([2]))[0]  # an away win
    assert far > near


def test_log_loss_and_brier():
    forecast = np.array([[0.5, 0.3, 0.2]])
    assert log_loss(forecast, np.array([0]))[0] == pytest.approx(-np.log(0.5))
    assert brier(forecast, np.array([0]))[0] == pytest.approx(0.25 + 0.09 + 0.04)


def test_log_loss_survives_a_zero_probability():
    assert np.isfinite(log_loss(np.array([[0.0, 0.5, 0.5]]), np.array([0]))[0])


def _predictions(rows):
    return pd.DataFrame(
        rows, columns=["match_id", "model", "p_home", "p_draw", "p_away", "outcome", "season"]
    )


def test_score_and_summarize_rank_models_by_rps():
    scored = score_predictions(
        _predictions(
            [
                (1, "good", 0.7, 0.2, 0.1, 0, 2025),
                (2, "good", 0.1, 0.2, 0.7, 2, 2025),
                (1, "bad", 0.2, 0.3, 0.5, 0, 2025),
                (2, "bad", 0.5, 0.3, 0.2, 2, 2025),
            ]
        )
    )
    table = summarize(scored)
    assert list(table.index) == ["good", "bad"]
    assert table.loc["good", "matches"] == 2
    assert table.loc["good", "accuracy"] == 1.0
    assert table.loc["bad", "accuracy"] == 0.0


def test_paired_difference_is_zero_for_identical_models_and_negative_for_a_better_one():
    rng = np.random.default_rng(0)
    probabilities = rng.dirichlet([4, 2, 3], size=400)
    outcomes = rng.integers(0, 3, size=400)
    rows = []
    for i, (p, o) in enumerate(zip(probabilities, outcomes, strict=True)):
        rows.append((i, "a", *p, o, 2025))
        rows.append((i, "same", *p, o, 2025))
        sharper = np.full(3, 0.1)
        sharper[o] = 0.8
        rows.append((i, "oracle", *sharper, o, 2025))
    scored = score_predictions(_predictions(rows))

    same = paired_difference(scored, "a", "same")
    assert same["mean"] == 0.0 and same["matches"] == 400

    better = paired_difference(scored, "oracle", "a")
    assert better["mean"] < 0 and better["ci_high"] < 0


def test_calibration_table_counts_matches_per_band():
    scored = score_predictions(
        _predictions(
            [
                (1, "m", 0.05, 0.45, 0.50, 2, 2025),
                (2, "m", 0.95, 0.03, 0.02, 0, 2025),
                (3, "m", 0.96, 0.02, 0.02, 1, 2025),
                (4, "m", 1.00, 0.00, 0.00, 0, 2025),
            ]
        )
    )
    table = calibration_table(scored, "m").set_index("band")
    assert table.loc["0.0-0.1", "matches"] == 1
    assert table.loc["0.9-1.0", "matches"] == 3
    assert table.loc["0.9-1.0", "observed"] == pytest.approx(2 / 3)
