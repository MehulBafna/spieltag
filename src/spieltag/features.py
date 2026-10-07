"""Point-in-time features: what was known about a match when its prediction was made.

The rule that keeps the evaluation honest: the features of a match may only use results that were
known at the match's `as_of_utc` time, which is `kickoff - lead` (24 hours by default, matching when
the production pipeline freezes its predictions). A result counts as known `RESULT_DELAY` after its
kickoff, a safe upper bound for a 90-minute match with half time and stoppage time.

The schedule (who plays when) is public long before kickoff, so rest days are computed from it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta

import numpy as np
import pandas as pd

from spieltag.ratings import EloConfig, EloRatings

HOME, DRAW, AWAY = 0, 1, 2
OUTCOME_LABELS = ("home", "draw", "away")

RESULT_DELAY = timedelta(hours=3)
DEFAULT_LEAD = timedelta(hours=24)

FORM_WINDOW = 5
NEUTRAL_POINTS_PER_MATCH = 1.375  # about the league average: 25% draws, 45% home wins
MAX_REST_DAYS = 14.0

KEY_COLUMNS = [
    "match_id",
    "season",
    "matchday",
    "kickoff_utc",
    "as_of_utc",
    "home_team_id",
    "away_team_id",
    "is_finished",
    "home_goals",
    "away_goals",
    "outcome",
]
FEATURE_COLUMNS = [
    "elo_home",
    "elo_away",
    "elo_diff",
    "form_home",
    "form_away",
    "goal_diff_form_home",
    "goal_diff_form_away",
    "rest_days_home",
    "rest_days_away",
]
# What the models see. The two raw ratings are kept in the table for reading, not for modelling.
MODEL_FEATURES = [c for c in FEATURE_COLUMNS if c not in ("elo_home", "elo_away")]

_RESULT, _PREDICT = 0, 1  # at the same moment, a result counts as known before a prediction is made


def outcome_from_goals(home_goals: pd.Series, away_goals: pd.Series) -> pd.Series:
    """0 = home win, 1 = draw, 2 = away win, missing while the match has no score."""
    outcome = pd.Series(pd.NA, index=home_goals.index, dtype="Int64")
    known = home_goals.notna() & away_goals.notna()
    home, away = home_goals[known].astype(int), away_goals[known].astype(int)
    outcome[known] = np.select([home > away, home == away], [HOME, DRAW], default=AWAY)
    return outcome


def _points(goals_for: int, goals_against: int) -> int:
    if goals_for > goals_against:
        return 3
    return 1 if goals_for == goals_against else 0


def _rest_days(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Days since each side's previous scheduled match, capped, for home and away."""
    sides = []
    for side, column in (("home", "home_team_id"), ("away", "away_team_id")):
        sides.append(
            pd.DataFrame(
                {
                    "team": frame[column].to_numpy(),
                    "kickoff": frame["kickoff_utc"].reset_index(drop=True),
                    "row": frame.index.to_numpy(),
                    "side": side,
                }
            )
        )
    long = pd.concat(sides).sort_values(["team", "kickoff", "row"])
    gap = long.groupby("team")["kickoff"].diff().dt.total_seconds() / 86400.0
    long["rest"] = gap.clip(upper=MAX_REST_DAYS).fillna(MAX_REST_DAYS)
    home = long[long["side"] == "home"].set_index("row")["rest"].reindex(frame.index)
    away = long[long["side"] == "away"].set_index("row")["rest"].reindex(frame.index)
    return home.to_numpy(dtype=float), away.to_numpy(dtype=float)


def build_features(
    matches: pd.DataFrame,
    *,
    lead: timedelta = DEFAULT_LEAD,
    elo_config: EloConfig | None = None,
    window: int = FORM_WINDOW,
) -> pd.DataFrame:
    """One row per match, finished or not, with features known at `as_of_utc`.

    The function replays history as a list of events in time order: a result event when a finished
    match becomes known, and a prediction event when a match's features are read off. A prediction
    event only sees the results that came before it, so no later result can leak in.
    """
    if lead < timedelta(0):
        raise ValueError("lead must not be negative")
    frame = matches.sort_values(["kickoff_utc", "match_id"]).reset_index(drop=True)
    count = len(frame)

    kickoff_ns = (
        frame["kickoff_utc"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy("datetime64[ns]")
    ).astype("int64")
    lead_ns = int(lead.total_seconds() * 1e9)
    delay_ns = int(RESULT_DELAY.total_seconds() * 1e9)

    seasons = frame["season"].to_numpy()
    home_ids = frame["home_team_id"].to_numpy()
    away_ids = frame["away_team_id"].to_numpy()
    finished = frame["is_finished"].to_numpy(dtype=bool)
    home_goals = frame["home_goals"].to_numpy(dtype="float64", na_value=np.nan)
    away_goals = frame["away_goals"].to_numpy(dtype="float64", na_value=np.nan)

    events: list[tuple[int, int, int]] = []
    for i in range(count):
        events.append((int(kickoff_ns[i]) - lead_ns, _PREDICT, i))
        if finished[i]:
            events.append((int(kickoff_ns[i]) + delay_ns, _RESULT, i))
    events.sort()

    ratings = EloRatings(elo_config)
    points: defaultdict[int, deque[int]] = defaultdict(lambda: deque(maxlen=window))
    margins: defaultdict[int, deque[int]] = defaultdict(lambda: deque(maxlen=window))
    current_season: int | None = None

    elo_home = np.full(count, np.nan)
    elo_away = np.full(count, np.nan)
    form_home = np.full(count, np.nan)
    form_away = np.full(count, np.nan)
    margin_home = np.full(count, np.nan)
    margin_away = np.full(count, np.nan)

    def form(team: int) -> float:
        seen = points[team]
        return (sum(seen) + (window - len(seen)) * NEUTRAL_POINTS_PER_MATCH) / window

    def margin(team: int) -> float:
        return sum(margins[team]) / window

    for _, kind, i in events:
        season = int(seasons[i])
        if current_season is None or season > current_season:
            ratings.start_season()
            current_season = season
        home, away = int(home_ids[i]), int(away_ids[i])
        if kind == _RESULT:
            hg, ag = int(home_goals[i]), int(away_goals[i])
            ratings.update(home, away, hg, ag)
            points[home].append(_points(hg, ag))
            points[away].append(_points(ag, hg))
            margins[home].append(hg - ag)
            margins[away].append(ag - hg)
        else:
            elo_home[i], elo_away[i] = ratings.rating(home), ratings.rating(away)
            form_home[i], form_away[i] = form(home), form(away)
            margin_home[i], margin_away[i] = margin(home), margin(away)

    rest_home, rest_away = _rest_days(frame)

    out = frame[
        [
            "match_id",
            "season",
            "matchday",
            "kickoff_utc",
            "home_team_id",
            "away_team_id",
            "is_finished",
            "home_goals",
            "away_goals",
        ]
    ].copy()
    out["as_of_utc"] = out["kickoff_utc"] - lead
    out["outcome"] = outcome_from_goals(out["home_goals"], out["away_goals"])
    out["elo_home"] = elo_home
    out["elo_away"] = elo_away
    out["elo_diff"] = elo_home - elo_away
    out["form_home"] = form_home
    out["form_away"] = form_away
    out["goal_diff_form_home"] = margin_home
    out["goal_diff_form_away"] = margin_away
    out["rest_days_home"] = rest_home
    out["rest_days_away"] = rest_away
    return out[KEY_COLUMNS + FEATURE_COLUMNS]
