"""Elo ratings for Bundesliga clubs.

A club's rating changes only when one of its matches has finished. The constants below are common
defaults and are deliberately not tuned on Bundesliga results, so the evaluation is not flattered by
settings picked after looking at the answers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EloConfig:
    k: float = 20.0  # how far one result moves a rating
    home_advantage: float = 65.0  # rating points added to the home side when predicting
    initial: float = 1500.0  # the league mean, and the start rating in the first season of the data
    newcomer: float = 1450.0  # start rating of a club that appears after the first season
    carry_over: float = 0.75  # share of the gap to the mean that survives the summer break
    scale: float = 400.0


def expected_home_score(rating_home: float, rating_away: float, config: EloConfig) -> float:
    """Expected result for the home side: 1 is a win, 0.5 a draw, 0 a defeat."""
    gap = rating_home + config.home_advantage - rating_away
    return 1.0 / (1.0 + 10.0 ** (-gap / config.scale))


def goal_multiplier(goal_difference: int) -> float:
    """Bigger wins move ratings more (the usual World Football Elo scale)."""
    margin = abs(goal_difference)
    if margin <= 1:
        return 1.0
    if margin == 2:
        return 1.5
    return (11.0 + margin) / 8.0


class EloRatings:
    """The current rating of every club, updated one finished match at a time."""

    def __init__(self, config: EloConfig | None = None) -> None:
        self.config = config or EloConfig()
        self._ratings: dict[int, float] = {}
        self._seasons_started = 0

    def start_season(self) -> None:
        """Pull every rating back towards the mean. Call once when a new season begins."""
        self._seasons_started += 1
        if self._seasons_started == 1:
            return
        mean, keep = self.config.initial, self.config.carry_over
        for team, rating in self._ratings.items():
            self._ratings[team] = mean + keep * (rating - mean)

    def rating(self, team_id: int) -> float:
        if team_id not in self._ratings:
            first_season = self._seasons_started <= 1
            self._ratings[team_id] = self.config.initial if first_season else self.config.newcomer
        return self._ratings[team_id]

    def update(self, home_id: int, away_id: int, home_goals: int, away_goals: int) -> None:
        home, away = self.rating(home_id), self.rating(away_id)
        expected = expected_home_score(home, away, self.config)
        if home_goals > away_goals:
            actual = 1.0
        elif home_goals == away_goals:
            actual = 0.5
        else:
            actual = 0.0
        change = self.config.k * goal_multiplier(home_goals - away_goals) * (actual - expected)
        self._ratings[home_id] = home + change
        self._ratings[away_id] = away - change
