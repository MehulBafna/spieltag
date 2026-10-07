"""A simulated clock for the production cycle.

`ClockFetcher` answers like OpenLigaDB would at a given moment: fixtures are always listed, and a
result appears only once the match is over. The simulated league is turned into the API's real JSON
shape, so a replay runs through the same parser and data contract as production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from spieltag.features import RESULT_DELAY
from spieltag.pipeline import run_ingest
from spieltag.store import LocalStore


def raw_match(row: pd.Series, *, now: datetime, reveal: bool = True) -> dict[str, Any]:
    over = reveal and row["kickoff_utc"] + RESULT_DELAY <= pd.Timestamp(now)
    results = (
        [
            {
                "resultTypeID": 2,
                "resultName": "Endergebnis",
                "pointsTeam1": int(row["home_goals"]),
                "pointsTeam2": int(row["away_goals"]),
            }
        ]
        if over
        else []
    )
    return {
        "matchID": int(row["match_id"]),
        "leagueSeason": int(row["season"]),
        "matchDateTimeUTC": row["kickoff_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group": {"groupOrderID": int(row["matchday"])},
        "team1": {"teamId": int(row["home_team_id"]), "teamName": row["home_team"]},
        "team2": {"teamId": int(row["away_team_id"]), "teamName": row["away_team"]},
        "matchIsFinished": bool(over),
        "matchResults": results,
    }


class ClockFetcher:
    """`fetcher(league, season)` for run_cycle, driven by a clock the test moves."""

    def __init__(self, matches: pd.DataFrame, now: datetime) -> None:
        self.matches = matches
        self.now = now
        self.calls = 0
        self.fail_with: Exception | None = None
        self.edit: Any = None  # optional function(list of raw matches) -> list, to corrupt data

    def __call__(self, league: str, season: int) -> list[dict[str, Any]]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        rows = self.matches[self.matches["season"] == season]
        raw = [raw_match(row, now=self.now) for _, row in rows.iterrows()]
        return self.edit(raw) if self.edit else raw


def hours(start: datetime, stop: datetime, step: int):
    """Clock times from `start` to `stop`, every `step` hours."""
    moment = start
    while moment <= stop:
        yield moment
        moment += timedelta(hours=step)


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def lake_with_history(matches: pd.DataFrame, root=None, *, league: str = "sim", store=None):
    """A store that holds the finished seasons before 2020, as it does after a backfill."""
    store = store or LocalStore(root)
    history_end = utc(2020, 7, 1)
    fetcher = ClockFetcher(matches, history_end)
    for season in range(2016, 2020):
        run_ingest(league, season, store, now=history_end, fetcher=fetcher)
    return store, fetcher
