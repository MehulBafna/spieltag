import pandas as pd
import pytest

from spieltag.contracts import ContractError, validate_matches
from spieltag.ingest import parse_matches


@pytest.fixture
def matches(raw_sample) -> pd.DataFrame:
    return parse_matches(raw_sample)


def test_a_valid_table_passes_unchanged(matches):
    assert validate_matches(matches).equals(matches)


def test_an_empty_table_passes():
    assert validate_matches(parse_matches([], league_season=2025)).empty


def test_duplicate_match_id_fails(matches):
    matches.loc[1, "match_id"] = matches.loc[0, "match_id"]
    with pytest.raises(ContractError, match="match_id"):
        validate_matches(matches)


def test_a_team_playing_itself_fails(matches):
    matches.loc[0, "away_team_id"] = matches.loc[0, "home_team_id"]
    with pytest.raises(ContractError, match="no_team_plays_itself"):
        validate_matches(matches)


def test_a_finished_match_without_a_score_fails(matches):
    matches.loc[0, "home_goals"] = pd.NA
    with pytest.raises(ContractError, match="finished_matches_have_a_score"):
        validate_matches(matches)


def test_an_unfinished_match_may_have_a_live_score(matches):
    matches.loc[2, ["home_goals", "away_goals"]] = [1, 0]
    validate_matches(matches)


def test_negative_goals_fail(matches):
    matches.loc[0, "home_goals"] = -1
    with pytest.raises(ContractError, match="home_goals"):
        validate_matches(matches)


@pytest.mark.parametrize("matchday", [0, 35])
def test_matchday_outside_1_to_34_fails(matches, matchday):
    matches.loc[0, "matchday"] = matchday
    with pytest.raises(ContractError, match="matchday"):
        validate_matches(matches)


def test_the_same_fixture_twice_in_a_season_fails(matches):
    matches.loc[3, ["home_team_id", "away_team_id"]] = [2, 3]
    matches.loc[3, ["home_team", "away_team"]] = ["Testclub Beta", "Testclub Gamma"]
    with pytest.raises(ContractError, match="each_fixture_once_per_season"):
        validate_matches(matches)


def test_a_blank_team_name_fails(matches):
    matches.loc[0, "home_team"] = ""
    with pytest.raises(ContractError, match="home_team"):
        validate_matches(matches)


def _many_matches(template: pd.Series, pairs: int, matchday) -> pd.DataFrame:
    rows = []
    for i in range(pairs):
        row = template.copy()
        row["match_id"] = 5000 + i
        row["home_team_id"] = 100 + 2 * i
        row["away_team_id"] = 101 + 2 * i
        row["matchday"] = matchday(i)
        rows.append(row)
    return pd.DataFrame(rows).astype(template.to_frame().T.dtypes.to_dict()).reset_index(drop=True)


def test_more_than_18_teams_in_a_season_fails(matches):
    table = _many_matches(matches.iloc[0], pairs=10, matchday=lambda i: i + 1)
    with pytest.raises(ContractError, match="at_most_18_teams_per_season"):
        validate_matches(table)


def test_more_than_9_matches_on_one_matchday_fails(matches):
    table = _many_matches(matches.iloc[0], pairs=10, matchday=lambda i: 1)
    with pytest.raises(ContractError, match="at_most_9_matches_per_matchday"):
        validate_matches(table)


def test_an_unexpected_extra_column_fails(matches):
    matches["surprise"] = 1
    with pytest.raises(ContractError, match="surprise"):
        validate_matches(matches)
