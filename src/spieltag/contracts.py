"""Data contracts: what a valid table of Bundesliga matches looks like.

The pipeline validates every ingest against these rules. Bad data stops the run, an alarm fires,
and the previous predictions stay online (see the blueprint).
"""

from __future__ import annotations

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

CLUBS_PER_SEASON = 18
MATCHDAYS_PER_SEASON = 34
MATCHES_PER_MATCHDAY = CLUBS_PER_SEASON // 2
MATCHES_PER_SEASON = CLUBS_PER_SEASON * (CLUBS_PER_SEASON - 1)


class ContractError(ValueError):
    """The match table broke one or more data contract rules."""


def _no_team_plays_itself(df: pd.DataFrame) -> bool:
    return bool((df["home_team_id"] != df["away_team_id"]).all())


def _finished_matches_have_a_score(df: pd.DataFrame) -> bool:
    finished = df[df["is_finished"].astype(bool)]
    return bool(finished[["home_goals", "away_goals"]].notna().all().all())


def _each_fixture_once_per_season(df: pd.DataFrame) -> bool:
    return not df.duplicated(subset=["season", "home_team_id", "away_team_id"]).any()


def _at_most_18_teams_per_season(df: pd.DataFrame) -> bool:
    home = df[["season", "home_team_id"]].rename(columns={"home_team_id": "team"})
    away = df[["season", "away_team_id"]].rename(columns={"away_team_id": "team"})
    teams = pd.concat([home, away])
    return bool((teams.groupby("season")["team"].nunique() <= CLUBS_PER_SEASON).all())


def _at_most_306_matches_per_season(df: pd.DataFrame) -> bool:
    return bool((df.groupby("season").size() <= MATCHES_PER_SEASON).all())


def _at_most_9_matches_per_matchday(df: pd.DataFrame) -> bool:
    return bool((df.groupby(["season", "matchday"]).size() <= MATCHES_PER_MATCHDAY).all())


_text = pa.Check.str_length(min_value=1)

MATCH_SCHEMA = pa.DataFrameSchema(
    {
        "match_id": pa.Column("int64", pa.Check.gt(0), unique=True),
        "season": pa.Column("int64", pa.Check.in_range(1963, 2100)),
        "matchday": pa.Column("int64", pa.Check.in_range(1, MATCHDAYS_PER_SEASON)),
        "kickoff_utc": pa.Column(pd.DatetimeTZDtype(tz="UTC")),
        "home_team_id": pa.Column("int64", pa.Check.gt(0)),
        "home_team": pa.Column(str, _text),
        "away_team_id": pa.Column("int64", pa.Check.gt(0)),
        "away_team": pa.Column(str, _text),
        "is_finished": pa.Column(bool),
        "home_goals": pa.Column("Int64", pa.Check.ge(0), nullable=True),
        "away_goals": pa.Column("Int64", pa.Check.ge(0), nullable=True),
    },
    checks=[
        pa.Check(_no_team_plays_itself, name="no_team_plays_itself"),
        pa.Check(_finished_matches_have_a_score, name="finished_matches_have_a_score"),
        pa.Check(_each_fixture_once_per_season, name="each_fixture_once_per_season"),
        pa.Check(_at_most_18_teams_per_season, name="at_most_18_teams_per_season"),
        pa.Check(_at_most_306_matches_per_season, name="at_most_306_matches_per_season"),
        pa.Check(_at_most_9_matches_per_matchday, name="at_most_9_matches_per_matchday"),
    ],
    strict=True,
)


def validate_matches(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` unchanged if it satisfies every rule, otherwise raise ContractError."""
    try:
        return MATCH_SCHEMA.validate(df, lazy=True)
    except SchemaErrors as exc:
        cases = exc.failure_cases.to_string(max_rows=15, max_colwidth=60)
        raise ContractError(f"match data broke the contract:\n{cases}") from exc
