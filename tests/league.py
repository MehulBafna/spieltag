"""A simulated Bundesliga for tests. Invented data, with club strengths that are known.

Goals are drawn from Poisson distributions whose rates depend on the strength gap between the two
clubs, so a model that works must be able to find that signal. With `strength_sd=0` every club is
equal, so no honest model can beat the league averages. That second case is the leakage canary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

CLUBS = 18


def _round_robin(teams: list[int]) -> list[list[tuple[int, int]]]:
    """Every club plays every other club home and away: 34 rounds of 9 matches."""
    n = len(teams)
    order = list(teams)
    first_half = []
    for round_number in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = order[i], order[n - 1 - i]
            pairs.append((a, b) if (round_number + i) % 2 == 0 else (b, a))
        first_half.append(pairs)
        order = [order[0], order[-1], *order[1:-1]]
    return first_half + [[(b, a) for a, b in pairs] for pairs in first_half]


# Friday evening, Saturday afternoon, Saturday evening, Sunday: the usual weekend shape.
_SLOTS = [(-1, 18), (0, 13), (0, 13), (0, 13), (0, 13), (0, 13), (0, 16), (1, 13), (1, 16)]


def simulate_league(
    seasons: range | list[int],
    *,
    strength_sd: float = 0.5,
    drift_sd: float = 0.1,
    seed: int = 7,
    unfinished_after: datetime | None = None,
) -> pd.DataFrame:
    """Matches in the same shape as the curated table. Later matches can be left unfinished."""
    rng = np.random.default_rng(seed)
    strength = rng.normal(0.0, strength_sd, CLUBS)
    teams = list(range(1, CLUBS + 1))
    rows = []
    for season in seasons:
        strength = strength + rng.normal(0.0, drift_sd, CLUBS) if strength_sd else strength
        rounds = _round_robin(teams)
        start = datetime(season, 8, 15, tzinfo=UTC)
        for matchday, pairs in enumerate(rounds, start=1):
            for slot, (home, away) in zip(_SLOTS, pairs, strict=True):
                day, hour = slot
                kickoff = start + timedelta(days=7 * (matchday - 1) + day, hours=hour)
                gap = strength[home - 1] - strength[away - 1]
                goals_home = int(rng.poisson(1.45 * np.exp(0.5 * gap)))
                goals_away = int(rng.poisson(1.15 * np.exp(-0.5 * gap)))
                finished = unfinished_after is None or kickoff < unfinished_after
                rows.append(
                    {
                        "match_id": season * 1000 + len(rows) % 1000 + 1,
                        "season": season,
                        "matchday": matchday,
                        "kickoff_utc": kickoff,
                        "home_team_id": home,
                        "home_team": f"Club {home:02d}",
                        "away_team_id": away,
                        "away_team": f"Club {away:02d}",
                        "is_finished": finished,
                        "home_goals": goals_home if finished else None,
                        "away_goals": goals_away if finished else None,
                    }
                )
    frame = pd.DataFrame(rows)
    frame["match_id"] = (frame["season"] * 1000 + frame.groupby("season").cumcount() + 1).astype(
        "int64"
    )
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    frame["home_goals"] = frame["home_goals"].astype("Int64")
    frame["away_goals"] = frame["away_goals"].astype("Int64")
    for column in ("season", "matchday", "home_team_id", "away_team_id"):
        frame[column] = frame[column].astype("int64")
    return frame
