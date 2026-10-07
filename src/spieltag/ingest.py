"""OpenLigaDB client and parser for Bundesliga match data.

OpenLigaDB is a free, community-run API (data under the ODbL licence, credit it in the README).
`GET /getmatchdata/{league}/{season}` returns every match of one season as a JSON list.
For the Bundesliga the league shortcut is `bl1` and the season is the year it starts in.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://api.openligadb.de"
USER_AGENT = "spieltag/0.1 (portfolio project)"

# OpenLigaDB lists several results per match (half time, full time, extra time, ...).
# resultTypeID 2 is "Endergebnis", the result after 90 minutes. We use only that one.
FULL_TIME_RESULT_TYPE_ID = 2

MATCH_COLUMNS = [
    "match_id",
    "season",
    "matchday",
    "kickoff_utc",
    "home_team_id",
    "home_team",
    "away_team_id",
    "away_team",
    "is_finished",
    "home_goals",
    "away_goals",
]


class OpenLigaError(RuntimeError):
    """The API did not answer, or answered in a shape this project does not expect."""


_MISSING = object()


def _get(obj: dict[str, Any], key: str, default: Any = _MISSING) -> Any:
    """Read `key` from a JSON object, ignoring letter case, with a readable error if absent."""
    if key in obj:
        return obj[key]
    wanted = key.lower()
    for name, value in obj.items():
        if name.lower() == wanted:
            return value
    if default is _MISSING:
        raise OpenLigaError(f"field {key!r} missing from API response; keys seen: {sorted(obj)}")
    return default


def current_season(today: date | None = None) -> int:
    """The Bundesliga season starts in August, so July onwards counts as the new season."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def fetch_season_raw(
    league: str,
    season: int,
    *,
    session: Any = None,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 2.0,
) -> list[dict[str, Any]]:
    """Download all matches of one season. Retries on network errors and 5xx answers only."""
    url = f"{BASE_URL}/getmatchdata/{league}/{season}"
    getter = session or requests
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = getter.get(url, timeout=timeout, headers=headers)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
        else:
            if response.status_code < 500:
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise OpenLigaError(f"{url} did not return JSON") from exc
                if not isinstance(payload, list):
                    raise OpenLigaError(f"{url} returned {type(payload).__name__}, expected a list")
                return payload
            last_error = OpenLigaError(f"{url} answered with status {response.status_code}")
        if attempt < retries:
            time.sleep(backoff * attempt)

    raise OpenLigaError(f"giving up on {url} after {retries} attempts") from last_error


def _full_time_score(results: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """Goals (home, away) after 90 minutes, or (None, None) if there is no such result yet."""
    for result in results:
        if _get(result, "resultTypeID", None) == FULL_TIME_RESULT_TYPE_ID:
            return int(_get(result, "pointsTeam1")), int(_get(result, "pointsTeam2"))
    for result in results:
        if str(_get(result, "resultName", "")).lower() == "endergebnis":
            return int(_get(result, "pointsTeam1")), int(_get(result, "pointsTeam2"))
    return None, None


def parse_matches(raw: list[dict[str, Any]], *, league_season: int | None = None) -> pd.DataFrame:
    """Turn the API's JSON into one tidy row per match."""
    rows = []
    for match in raw:
        team1 = _get(match, "team1")
        team2 = _get(match, "team2")
        home_goals, away_goals = _full_time_score(_get(match, "matchResults", None) or [])
        rows.append(
            {
                "match_id": int(_get(match, "matchID")),
                "season": int(_get(match, "leagueSeason", league_season)),
                "matchday": int(_get(_get(match, "group"), "groupOrderID")),
                "kickoff_utc": _get(match, "matchDateTimeUTC"),
                "home_team_id": int(_get(team1, "teamId")),
                "home_team": str(_get(team1, "teamName")),
                "away_team_id": int(_get(team2, "teamId")),
                "away_team": str(_get(team2, "teamName")),
                "is_finished": bool(_get(match, "matchIsFinished")),
                "home_goals": home_goals,
                "away_goals": away_goals,
            }
        )

    frame = pd.DataFrame(rows, columns=MATCH_COLUMNS)
    kickoff = pd.to_datetime(frame["kickoff_utc"], utc=True)
    frame["kickoff_utc"] = kickoff.astype("datetime64[ns, UTC]")
    return frame.astype(
        {
            "match_id": "int64",
            "season": "int64",
            "matchday": "int64",
            "home_team_id": "int64",
            "away_team_id": "int64",
            "is_finished": "bool",
            "home_goals": "Int64",
            "away_goals": "Int64",
        }
    )
