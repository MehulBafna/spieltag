from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from spieltag import features as features_module
from spieltag.features import (
    FEATURE_COLUMNS,
    MODEL_FEATURES,
    NEUTRAL_POINTS_PER_MATCH,
    RESULT_DELAY,
    build_features,
    outcome_from_goals,
)


def at(day: int, hour: int = 15) -> datetime:
    return datetime(2025, 8, day, hour, tzinfo=UTC)


def make_matches(rows: list[tuple]) -> pd.DataFrame:
    """rows: (match_id, matchday, kickoff, home, away, home_goals, away_goals or None)."""
    frame = pd.DataFrame(
        [
            {
                "match_id": match_id,
                "season": 2025,
                "matchday": matchday,
                "kickoff_utc": kickoff,
                "home_team_id": home,
                "home_team": f"Club {home}",
                "away_team_id": away,
                "away_team": f"Club {away}",
                "is_finished": goals_home is not None,
                "home_goals": goals_home,
                "away_goals": goals_away,
            }
            for match_id, matchday, kickoff, home, away, goals_home, goals_away in rows
        ]
    )
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    frame["home_goals"] = frame["home_goals"].astype("Int64")
    frame["away_goals"] = frame["away_goals"].astype("Int64")
    return frame


SMALL = [
    (1, 1, at(1), 1, 2, 3, 0),
    (2, 1, at(1), 3, 4, 1, 1),
    (3, 2, at(8), 1, 3, None, None),
    (4, 2, at(8), 4, 2, None, None),
]


def row(features: pd.DataFrame, match_id: int) -> pd.Series:
    return features.set_index("match_id").loc[match_id]


def test_the_first_matches_of_a_club_get_neutral_features():
    first = row(build_features(make_matches(SMALL)), 1)
    assert first["elo_home"] == first["elo_away"] == 1500.0
    assert first["form_home"] == NEUTRAL_POINTS_PER_MATCH
    assert first["goal_diff_form_home"] == 0.0
    assert first["rest_days_home"] == 14.0


def test_a_known_result_shows_up_in_the_next_features_of_both_clubs():
    features = build_features(make_matches(SMALL))
    third = row(features, 3)  # club 1 (won 3-0) hosts club 3 (drew 1-1)
    assert third["elo_home"] > 1500
    assert third["elo_diff"] > 0
    assert third["form_home"] == pytest.approx((3 + 4 * NEUTRAL_POINTS_PER_MATCH) / 5)
    assert third["goal_diff_form_home"] == pytest.approx(3 / 5)
    assert third["form_away"] == pytest.approx((1 + 4 * NEUTRAL_POINTS_PER_MATCH) / 5)
    assert third["rest_days_home"] == pytest.approx(7.0)


def test_unfinished_matches_get_features_but_never_change_anything():
    rows = [*SMALL, (5, 3, at(15), 1, 3, None, None)]
    features = build_features(make_matches(rows))
    without_unfinished = build_features(make_matches([r for r in rows if r[0] != 3]))
    assert pd.isna(row(features, 3)["outcome"])
    assert row(features, 5)["elo_home"] == row(without_unfinished, 5)["elo_home"]
    assert row(features, 5)["form_home"] == row(without_unfinished, 5)["form_home"]


def test_a_result_counts_only_once_it_can_be_known():
    # Club 1 plays at 12:00 and again 2 hours later (not realistic, but it isolates the rule).
    rows = [(1, 1, at(1, 12), 1, 2, 3, 0), (2, 1, at(1, 14), 1, 3, 0, 0)]
    soon = build_features(make_matches(rows), lead=timedelta(0))
    assert row(soon, 2)["elo_home"] == 1500.0  # the first result is not known yet

    rows[1] = (2, 1, at(1, 12) + RESULT_DELAY + timedelta(hours=1), 1, 3, 0, 0)
    later = build_features(make_matches(rows), lead=timedelta(0))
    assert row(later, 2)["elo_home"] > 1500.0


def test_a_longer_lead_hides_results_that_a_shorter_one_would_use():
    rows = [(1, 1, at(1, 12), 1, 2, 3, 0), (2, 2, at(2, 13), 1, 3, None, None)]
    short = build_features(make_matches(rows), lead=timedelta(hours=1))
    long = build_features(make_matches(rows), lead=timedelta(hours=24))
    assert row(short, 2)["elo_home"] > 1500.0
    assert row(long, 2)["elo_home"] == 1500.0


def test_the_table_has_one_row_per_match_and_no_missing_features():
    features = build_features(make_matches(SMALL))
    assert len(features) == 4
    assert features[FEATURE_COLUMNS].notna().all().all()
    assert set(MODEL_FEATURES) < set(FEATURE_COLUMNS)


def test_outcomes_are_coded_0_1_2_and_missing_when_unplayed():
    goals_home = pd.Series([2, 1, 0, None], dtype="Int64")
    goals_away = pd.Series([0, 1, 3, None], dtype="Int64")
    assert outcome_from_goals(goals_home, goals_away).tolist() == [0, 1, 2, pd.NA]


def test_a_negative_lead_is_rejected():
    with pytest.raises(ValueError, match="lead"):
        build_features(make_matches(SMALL), lead=timedelta(hours=-1))


# --- Leakage tests. These are the point of the whole evaluation. ---------------------------------


def _change_results(matches: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    changed = matches.copy()
    changed.loc[mask & changed["is_finished"], "home_goals"] += 3
    return changed


def _unknown_results_change_rows(matches: pd.DataFrame, lead: timedelta) -> bool:
    """True if changing results that were not yet known changes a row that was read off earlier."""
    cutoff = matches["kickoff_utc"].sort_values().iloc[1200]
    original = build_features(matches, lead=lead)
    changed = build_features(_change_results(matches, matches["kickoff_utc"] >= cutoff), lead=lead)

    # Every changed result becomes known at or after `cutoff + RESULT_DELAY`, so every row whose
    # features are read off before that moment has to be identical.
    safe = original["as_of_utc"] < cutoff + RESULT_DELAY
    assert safe.sum() > 500
    same = (
        original[safe][FEATURE_COLUMNS]
        .reset_index(drop=True)
        .equals(changed[safe][FEATURE_COLUMNS].reset_index(drop=True))
    )
    # ...and the check is not vacuous: later rows do react to the changed results.
    assert not original[~safe][FEATURE_COLUMNS].equals(changed[~safe][FEATURE_COLUMNS])
    return not same


@pytest.mark.parametrize("lead_hours", [0, 24, 72])
def test_no_result_that_is_unknown_at_prediction_time_changes_a_row(league_matches, lead_hours):
    assert not _unknown_results_change_rows(league_matches, timedelta(hours=lead_hours))


def test_the_leakage_check_catches_a_feature_builder_that_leaks(league_matches, monkeypatch):
    """A test of the test: let results count 100 hours before kickoff and the check must object."""
    monkeypatch.setattr(features_module, "RESULT_DELAY", timedelta(hours=-100))
    assert _unknown_results_change_rows(league_matches, timedelta(hours=24))


@pytest.mark.parametrize("lead_hours", [0, 24])
def test_a_matchs_own_result_never_reaches_its_own_features(league_matches, lead_hours):
    lead = timedelta(hours=lead_hours)
    original = build_features(league_matches, lead=lead)
    target = league_matches.sort_values(["kickoff_utc", "match_id"]).iloc[800]
    own = league_matches["match_id"] == target["match_id"]

    changed = build_features(_change_results(league_matches, own), lead=lead)

    assert row(original, target["match_id"])[FEATURE_COLUMNS].equals(
        row(changed, target["match_id"])[FEATURE_COLUMNS]
    )


def test_features_do_react_to_earlier_results(league_matches):
    original = build_features(league_matches)
    early = league_matches["kickoff_utc"] < league_matches["kickoff_utc"].sort_values().iloc[100]
    changed = build_features(_change_results(league_matches, early))
    assert not original["elo_diff"].equals(changed["elo_diff"])


def test_as_of_time_is_kickoff_minus_lead(league_matches):
    features = build_features(league_matches, lead=timedelta(hours=24))
    assert (features["kickoff_utc"] - features["as_of_utc"] == timedelta(hours=24)).all()


def test_rows_come_back_in_kickoff_order(league_matches):
    shuffled = league_matches.sample(frac=1.0, random_state=3)
    features = build_features(shuffled)
    assert features["kickoff_utc"].is_monotonic_increasing
    pd.testing.assert_frame_equal(features, build_features(league_matches))
