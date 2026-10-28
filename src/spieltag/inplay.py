"""The in-play estimate: how a sealed forecast changes as goals are scored.

This module never touches the sealed forecasts. It reads them, turns each one into an expected
number of goals per team, and then answers one question every few minutes: given the score and the
minute, what is the chance of a home win, a draw and an away win?

The model is a Poisson goal process:

1. Rates. The sealed home, draw and away probabilities are turned into expected goals for the whole
   match (`fit_rates`). Nothing is learned from the match itself.
2. Timing. A share of those goals falls in each minute (`minute_weights`). By default 45 percent
   come in the first half and 55 percent in the second, which `spieltag-inplay-fit` can replace by
   a value estimated from past goal times.
3. Game state. A team's scoring rate is multiplied by a factor that depends on whether it is
   trailing or leading (`StateFactors`). The default is 1 everywhere, which is plain Poisson.
4. Remaining goals. The chance of every possible number of further goals for each team is added to
   the current score to give the final-result probabilities (`outcome_probabilities`).

What it cannot see: the feed has no cards, substitutions or injuries, and no match clock. The
minute is estimated from the kickoff time (`estimate_minute`) and can be off by a few minutes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

from spieltag.frozen import check_record, frozen_prefix, iso, parse_time
from spieltag.ingest import current_season, fetch_season_raw
from spieltag.metrics import PROBABILITY_COLUMNS, brier, rps
from spieltag.models import PRIMARY_MODEL
from spieltag.observability import log_event
from spieltag.store import Store

# ---------------------------------------------------------------------------------------------
# Settings. The values marked "assumption" are starting points, not facts.
# ---------------------------------------------------------------------------------------------

FULL_TIME = 90
FIRST_HALF_SHARE = 0.45  # assumption: share of a match's goals in the first 45 minutes
STOPPAGE_STEPS = 2  # assumption: minutes still to play once the clock passes 90
FIRST_HALF_END_ELAPSED = 47  # minutes after kickoff at which we assume the first half is over
HALF_TIME_BREAK = 15  # minutes between the halves
MAX_GOALS = 12  # further goals per team that are tracked; the rest has no measurable mass
CHECKPOINTS = (15, 30, 45, 60, 75)
CHECKPOINT_TOLERANCE = 3  # a checkpoint uses the stored estimate within this many minutes

CANDIDATE_WINDOW = timedelta(hours=4, minutes=30)  # a kicked-off match is followed this long
FINALISE_WINDOW = timedelta(hours=48)  # an unfinished timeline is still checked this long
SHOW_WINDOW = timedelta(hours=36)  # matches this recent are shown on the dashboard
MAX_POINTS = 120

STATE_NAMES = ("trail2", "trail1", "level", "lead1", "lead2")


def inplay_prefix(league: str, season: int) -> str:
    return f"inplay/league={league}/season={season}/"


def inplay_key(league: str, season: int, match_id: int) -> str:
    return f"{inplay_prefix(league, season)}match_id={match_id}.json"


def params_key(league: str) -> str:
    return f"meta/league={league}/inplay_params.json"


# ---------------------------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StateFactors:
    """Rate multipliers by the score from a team's own point of view. Level is always 1."""

    trail2: float = 1.0
    trail1: float = 1.0
    lead1: float = 1.0
    lead2: float = 1.0

    def lookup(self, goal_difference: np.ndarray) -> np.ndarray:
        """Factor for each own-minus-opponent goal difference."""
        d = np.asarray(goal_difference)
        return np.where(
            d <= -2,
            self.trail2,
            np.where(
                d == -1,
                self.trail1,
                np.where(d == 0, 1.0, np.where(d == 1, self.lead1, self.lead2)),
            ),
        )

    @property
    def neutral(self) -> bool:
        return self.trail2 == self.trail1 == self.lead1 == self.lead2 == 1.0


@dataclass(frozen=True)
class InplayParams:
    first_half_share: float = FIRST_HALF_SHARE
    factors: StateFactors = field(default_factory=StateFactors)
    source: str = "defaults"

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_half_share": self.first_half_share,
            "factors": asdict(self.factors),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> InplayParams:
        factors = body.get("factors", {})
        values = {
            name: float(factors.get(name, 1.0)) for name in ("trail2", "trail1", "lead1", "lead2")
        }
        share = float(body.get("first_half_share", FIRST_HALF_SHARE))
        if not 0.2 <= share <= 0.8:
            raise ValueError(f"first_half_share {share} is not plausible")
        if any(not 0.3 <= v <= 3.0 for v in values.values()):
            raise ValueError(f"state factors {values} are not plausible")
        return cls(share, StateFactors(**values), str(body.get("source", "file")))


def load_params(store: Store, league: str) -> InplayParams:
    """The fitted parameters if a valid file exists, otherwise the neutral defaults."""
    try:
        return InplayParams.from_dict(json.loads(store.get_bytes(params_key(league))))
    except FileNotFoundError:
        return InplayParams()
    except (ValueError, TypeError, KeyError) as exc:
        log_event("inplay_params_invalid", error=f"{type(exc).__name__}: {exc}")
        return InplayParams(source="defaults (file invalid)")


# ---------------------------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------------------------


def minute_weights(first_half_share: float = FIRST_HALF_SHARE) -> np.ndarray:
    """Share of a match's goals in each of the 90 minutes. Sums to 1."""
    first = np.full(45, first_half_share / 45)
    second = np.full(45, (1.0 - first_half_share) / 45)
    return np.concatenate([first, second])


def remaining_weights(minute: int, first_half_share: float = FIRST_HALF_SHARE) -> np.ndarray:
    """Weights of the minutes still to play. Past 90 minutes, a short stoppage is assumed."""
    weights = minute_weights(first_half_share)
    if minute >= FULL_TIME:
        return np.full(STOPPAGE_STEPS, weights[-1])
    return weights[max(minute, 0) :]


def _match_outcome_probabilities(lam_home: float, lam_away: float) -> np.ndarray:
    """(home, draw, away) for two independent Poisson goal counts."""
    goals = np.arange(MAX_GOALS + 1)
    joint = np.outer(poisson.pmf(goals, lam_home), poisson.pmf(goals, lam_away))
    diff = goals[:, None] - goals[None, :]
    result = np.array([joint[diff > 0].sum(), joint[diff == 0].sum(), joint[diff < 0].sum()])
    return result / result.sum()


def fit_rates(p_home: float, p_draw: float, p_away: float) -> tuple[float, float, float]:
    """Expected goals (home, away) for 90 minutes that reproduce the sealed probabilities.

    Returns the two rates and the largest absolute gap between the sealed probabilities and the
    ones these rates give. Independent Poisson goals cannot match every triple exactly, so the gap
    is reported, not hidden.
    """
    target = np.clip(np.array([p_home, p_draw, p_away], dtype=float), 1e-6, None)
    target = target / target.sum()

    def loss(x: np.ndarray) -> float:
        got = _match_outcome_probabilities(math.exp(x[0]), math.exp(x[1]))
        return float(((got - target) ** 2).sum())

    start = np.log([1.5, 1.2])
    result = minimize(loss, start, method="L-BFGS-B", bounds=[(math.log(0.05), math.log(6.0))] * 2)
    lam_home, lam_away = math.exp(result.x[0]), math.exp(result.x[1])
    gap = float(np.abs(_match_outcome_probabilities(lam_home, lam_away) - target).max())
    return lam_home, lam_away, gap


def remaining_goal_distribution(
    lam_home: float,
    lam_away: float,
    minute: int,
    goal_difference: int,
    params: InplayParams,
) -> np.ndarray:
    """P[a, b]: chance that home scores `a` and away scores `b` more goals from `minute` on.

    With neutral factors this is two independent Poisson counts. Otherwise the rest of the match
    is stepped through minute by minute, and each team's rate depends on the score at that moment.
    """
    weights = remaining_weights(minute, params.first_half_share)
    size = MAX_GOALS + 1
    goals = np.arange(size)

    if params.factors.neutral:
        share = float(weights.sum())
        joint = np.outer(poisson.pmf(goals, lam_home * share), poisson.pmf(goals, lam_away * share))
        return joint / joint.sum()

    added_home, added_away = np.indices((size, size))
    diff = goal_difference + added_home - added_away
    home_factor = params.factors.lookup(diff)
    away_factor = params.factors.lookup(-diff)

    dist = np.zeros((size, size))
    dist[0, 0] = 1.0
    for weight in weights:
        mu_home = lam_home * weight * home_factor
        mu_away = lam_away * weight * away_factor
        p_home = _step_pmf(mu_home)
        p_away = _step_pmf(mu_away)
        new = np.zeros_like(dist)
        for i in range(3):
            for j in range(3):
                moved = dist * p_home[i] * p_away[j]
                new[i:, j:] += moved[: size - i, : size - j]
        dist = new
    return dist / dist.sum()


def _step_pmf(mu: np.ndarray) -> list[np.ndarray]:
    """Chance of 0, 1 or 2-or-more goals in one minute, for an array of rates."""
    p0 = np.exp(-mu)
    p1 = mu * p0
    return [p0, p1, np.clip(1.0 - p0 - p1, 0.0, 1.0)]


def outcome_probabilities(
    lam_home: float,
    lam_away: float,
    minute: int,
    score: tuple[int, int],
    params: InplayParams | None = None,
) -> tuple[np.ndarray, float, float]:
    """(home, draw, away) probabilities now, plus the expected further goals for each team."""
    params = params or InplayParams()
    home, away = score
    dist = remaining_goal_distribution(lam_home, lam_away, minute, home - away, params)
    size = dist.shape[0]
    added_home, added_away = np.indices((size, size))
    final_diff = (home - away) + added_home - added_away
    probabilities = np.array(
        [dist[final_diff > 0].sum(), dist[final_diff == 0].sum(), dist[final_diff < 0].sum()]
    )
    expected_home = float((dist * added_home).sum())
    expected_away = float((dist * added_away).sum())
    return probabilities, expected_home, expected_away


# ---------------------------------------------------------------------------------------------
# What the feed tells us
# ---------------------------------------------------------------------------------------------


def _field(obj: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in obj:
        return obj[key]
    wanted = key.lower()
    for name, value in obj.items():
        if name.lower() == wanted:
            return value
    return default


def goal_events(raw_match: dict[str, Any]) -> list[tuple[int, int, int]]:
    """(minute, home score, away score) after each goal, in the order the goals were scored."""
    events = []
    for goal in _field(raw_match, "goals", None) or []:
        home = _field(goal, "scoreTeam1")
        away = _field(goal, "scoreTeam2")
        if home is None or away is None:
            continue
        minute = _field(goal, "matchMinute")
        minute = 0 if minute is None else int(minute)
        events.append((minute, int(home), int(away), int(_field(goal, "goalID", 0) or 0)))
    events.sort(key=lambda e: (e[1] + e[2], e[3], e[0]))
    return [(m, h, a) for m, h, a, _ in events]


def current_score(raw_match: dict[str, Any]) -> tuple[int, int]:
    events = goal_events(raw_match)
    return (events[-1][1], events[-1][2]) if events else (0, 0)


def final_score(raw_match: dict[str, Any]) -> tuple[int, int] | None:
    """The result after 90 minutes (OpenLigaDB result type 2), if it is listed."""
    for result in _field(raw_match, "matchResults", None) or []:
        if _field(result, "resultTypeID") == 2:
            return int(_field(result, "pointsTeam1")), int(_field(result, "pointsTeam2"))
    return None


def estimate_minute(kickoff: pd.Timestamp, now: datetime) -> tuple[int, str]:
    """(match minute, phase) from the wall clock. The feed has no clock, so this is an estimate.

    Phases: first_half, half_time, second_half. The first half is assumed to last 47 minutes of
    wall clock (45 plus a little stoppage), followed by a 15 minute break.
    """
    elapsed = (pd.Timestamp(now) - kickoff).total_seconds() / 60.0
    if elapsed < 0:
        return 0, "before_kickoff"
    if elapsed < FIRST_HALF_END_ELAPSED:
        return int(min(elapsed, 45)), "first_half"
    if elapsed < FIRST_HALF_END_ELAPSED + HALF_TIME_BREAK:
        return 45, "half_time"
    second = elapsed - (FIRST_HALF_END_ELAPSED + HALF_TIME_BREAK)
    return int(45 + second), "second_half"


# ---------------------------------------------------------------------------------------------
# One match's timeline
# ---------------------------------------------------------------------------------------------


def _round_probabilities(values: Sequence[float]) -> list[float]:
    return [round(float(v), 5) for v in values]


def new_timeline(record: dict[str, Any], params: InplayParams) -> dict[str, Any]:
    sealed = record["models"][PRIMARY_MODEL]
    probabilities = [sealed[column] for column in PROBABILITY_COLUMNS]
    lam_home, lam_away, gap = fit_rates(*probabilities)
    return {
        "schema_version": 1,
        "match_id": int(record["match_id"]),
        "season": int(record["season"]),
        "matchday": int(record["matchday"]),
        "home_team": record["home_team"],
        "away_team": record["away_team"],
        "kickoff_utc": record["kickoff_utc"],
        "sealed": dict(zip(PROBABILITY_COLUMNS, probabilities, strict=True)),
        "sealed_fingerprint": record["fingerprint"],
        "rates": {
            "home": round(lam_home, 4),
            "away": round(lam_away, 4),
            "fit_gap": round(gap, 4),
        },
        "params": params.to_dict(),
        "points": [],
        "final": None,
    }


def add_point(
    timeline: dict[str, Any],
    *,
    now: datetime,
    score: tuple[int, int],
    minute: int,
    phase: str,
    feed_updated: str | None,
    params: InplayParams,
    finished: bool = False,
) -> dict[str, Any]:
    """Append the estimate for this moment. A second point for the same time replaces the first."""
    probabilities, exp_home, exp_away = outcome_probabilities(
        timeline["rates"]["home"], timeline["rates"]["away"], minute, score, params
    )
    if finished:
        diff = score[0] - score[1]
        probabilities = np.array([diff > 0, diff == 0, diff < 0], dtype=float)
        exp_home = exp_away = 0.0
    point = {
        "t": iso(now),
        "minute": int(minute),
        "phase": "full_time" if finished else phase,
        "home": int(score[0]),
        "away": int(score[1]),
        "p_home": round(float(probabilities[0]), 5),
        "p_draw": round(float(probabilities[1]), 5),
        "p_away": round(float(probabilities[2]), 5),
        "exp_final_home": round(score[0] + exp_home, 3),
        "exp_final_away": round(score[1] + exp_away, 3),
        "feed_updated": feed_updated,
    }
    points = [p for p in timeline["points"] if p["t"] != point["t"]]
    points.append(point)
    timeline["points"] = points[-MAX_POINTS:]
    return point


def finalise(timeline: dict[str, Any], score: tuple[int, int], now: datetime) -> None:
    diff = score[0] - score[1]
    timeline["final"] = {
        "home": int(score[0]),
        "away": int(score[1]),
        "outcome": 0 if diff > 0 else (1 if diff == 0 else 2),
        "at": iso(now),
    }


# ---------------------------------------------------------------------------------------------
# Scoring the live line against the sealed line, at fixed minutes
# ---------------------------------------------------------------------------------------------


def checkpoint_table(timelines: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each checkpoint minute: how the live estimate scored against the sealed forecast.

    Only finished matches count, and a match counts at a checkpoint only if a stored estimate lies
    within a few minutes of it. Lower scores are better; `gain` is 1 - live / sealed.
    """
    finished = [t for t in timelines if t.get("final")]
    rows = []
    for checkpoint in CHECKPOINTS:
        sealed, live, outcomes = [], [], []
        for timeline in finished:
            candidates = [
                p
                for p in timeline["points"]
                if abs(p["minute"] - checkpoint) <= CHECKPOINT_TOLERANCE
                and p["phase"] != "full_time"
            ]
            if not candidates:
                continue
            point = min(candidates, key=lambda p: abs(p["minute"] - checkpoint))
            sealed.append([timeline["sealed"][c] for c in PROBABILITY_COLUMNS])
            live.append([point[c] for c in PROBABILITY_COLUMNS])
            outcomes.append(timeline["final"]["outcome"])
        row: dict[str, Any] = {"minute": checkpoint, "matches": len(outcomes)}
        if outcomes:
            sealed_a, live_a = np.array(sealed), np.array(live)
            outcome_a = np.array(outcomes)
            rps_sealed, rps_live = rps(sealed_a, outcome_a), rps(live_a, outcome_a)
            gap = rps_live - rps_sealed
            row.update(
                rps_sealed=float(rps_sealed.mean()),
                rps_live=float(rps_live.mean()),
                brier_sealed=float(brier(sealed_a, outcome_a).mean()),
                brier_live=float(brier(live_a, outcome_a).mean()),
                gain=float(1.0 - rps_live.mean() / rps_sealed.mean()),
                live_better=int((gap < 0).sum()),
            )
            if len(gap) >= 2:
                half = 1.96 * float(gap.std(ddof=1)) / math.sqrt(len(gap))
                row.update(diff_low=float(gap.mean() - half), diff_high=float(gap.mean() + half))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------
# The scheduled tick
# ---------------------------------------------------------------------------------------------


@dataclass
class TickResult:
    now: str
    candidates: int = 0
    live: int = 0
    finished_now: int = 0
    feed_error: str | None = None
    published: bool = False
    problems: list[str] = field(default_factory=list)

    def metrics(self) -> dict[str, tuple[float, str]]:
        count = "Count"
        metrics: dict[str, tuple[float, str]] = {"InplayTickCompleted": (1, count)}
        if self.candidates:
            metrics["InplayLive"] = (self.live, count)
            metrics["InplayFeedFailed"] = (1 if self.feed_error else 0, count)
        return metrics

    def summary(self) -> dict[str, Any]:
        return asdict(self)


# Sealed records never change, so a warm Lambda container reads each one from S3 only once.
_SEALED_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _store_id(store: Store) -> str:
    return str(getattr(store, "bucket", None) or getattr(store, "root", "") or id(store))


def load_sealed(store: Store, league: str, season: int) -> tuple[list[dict[str, Any]], list[str]]:
    """The sound sealed records of a season, and the problems found. Read-only."""
    sid = _store_id(store)
    keys = store.list_keys(frozen_prefix(league, season))
    missing = [k for k in keys if (sid, k) not in _SEALED_CACHE]

    def read(key: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            return key, json.loads(store.get_bytes(key)), None
        except (FileNotFoundError, ValueError) as exc:
            return key, None, f"{type(exc).__name__}: {exc}"

    problems: list[str] = []
    if missing:
        with ThreadPoolExecutor(max_workers=16) as pool:
            for key, record, error in pool.map(read, missing):
                issues = [f"unreadable ({error})"] if record is None else check_record(record)
                if issues:
                    problems.append(f"{key}: {'; '.join(issues)}")
                else:
                    _SEALED_CACHE[(sid, key)] = record  # type: ignore[assignment]
    records = [_SEALED_CACHE[(sid, k)] for k in keys if (sid, k) in _SEALED_CACHE]
    return records, problems


def _read_json(store: Store, key: str) -> dict[str, Any] | None:
    try:
        return json.loads(store.get_bytes(key))
    except (FileNotFoundError, ValueError):
        return None


def _load_timelines(store: Store, league: str, season: int) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for key in store.list_keys(inplay_prefix(league, season)):
        body = _read_json(store, key)
        if body and "match_id" in body:
            result[int(body["match_id"])] = body
    return result


def run_inplay_tick(
    store: Store,
    *,
    league: str = "bl1",
    now: datetime,
    fetcher: Callable[[str, int], list[dict[str, Any]]] | None = None,
) -> TickResult:
    """One 5-minute tick: update every sealed match that is in play, and publish the result."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    fetch = fetcher or (lambda lg, season: fetch_season_raw(lg, season, retries=2, timeout=15.0))
    season = current_season(now.date())
    result = TickResult(now=iso(now))
    now_ts = pd.Timestamp(now).tz_convert("UTC")

    records, problems = load_sealed(store, league, season)
    result.problems = problems[:5]
    timelines = _load_timelines(store, league, season)

    def due(record: dict[str, Any]) -> bool:
        kickoff = parse_time(record["kickoff_utc"])
        age = now_ts - kickoff
        if age < timedelta(0):
            return False
        existing = timelines.get(int(record["match_id"]))
        if existing and existing.get("final"):
            return False  # closed with its result, nothing more to do
        if age <= CANDIDATE_WINDOW:
            return True
        return bool(existing and age <= FINALISE_WINDOW)

    candidates = [r for r in records if due(r)]
    result.candidates = len(candidates)
    if not candidates:
        return result

    params = load_params(store, league)
    raw_by_id: dict[int, dict[str, Any]] = {}
    try:
        for raw_match in fetch(league, season):
            raw_by_id[int(_field(raw_match, "matchID"))] = raw_match
    except Exception as exc:  # the feed is a volunteer service; keep the last estimates on screen
        result.feed_error = f"{type(exc).__name__}: {exc}"
        log_event("inplay_feed_failed", error=result.feed_error)

    for record in candidates:
        match_id = int(record["match_id"])
        raw_match = raw_by_id.get(match_id)
        if raw_match is None:
            continue
        kickoff = parse_time(record["kickoff_utc"])
        timeline = timelines.get(match_id) or new_timeline(record, params)
        finished = bool(_field(raw_match, "matchIsFinished", False))
        score = final_score(raw_match) if finished else None
        score = score or current_score(raw_match)
        minute, phase = estimate_minute(kickoff, now)
        if timeline.get("final") and finished:
            continue
        add_point(
            timeline,
            now=now,
            score=score,
            minute=min(minute, 90) if finished else minute,
            phase=phase,
            feed_updated=_field(raw_match, "lastUpdateDateTime"),
            params=params,
            finished=finished,
        )
        if finished:
            finalise(timeline, score, now)
            result.finished_now += 1
        else:
            result.live += 1
        store.put_bytes(
            inplay_key(league, season, match_id),
            json.dumps(timeline, separators=(",", ":"), ensure_ascii=False).encode(),
            content_type="application/json",
        )
        timelines[match_id] = timeline

    payload = inplay_summary(list(timelines.values()), params, now=now)
    store.put_bytes(
        "site/data/inplay.json",
        json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False).encode(),
        content_type="application/json",
        cache_control="public, max-age=30",
    )
    result.published = True
    log_event("inplay_tick", **{k: v for k, v in result.summary().items() if k != "problems"})
    return result


def inplay_summary(
    timelines: Sequence[dict[str, Any]], params: InplayParams, *, now: datetime
) -> dict[str, Any]:
    """What the dashboard reads: recent matches with their history, and the checkpoint scores."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    recent = [
        t for t in timelines if t["points"] and now_ts - parse_time(t["kickoff_utc"]) <= SHOW_WINDOW
    ]
    recent.sort(key=lambda t: (t["kickoff_utc"], t["match_id"]))
    return {
        "generated_at": iso(now),
        "model": "poisson_goal_process",
        "minute_is_estimated": True,
        "inputs": ["score", "estimated minute", "sealed pre-match forecast"],
        "not_used": ["red cards", "substitutions", "injuries", "possession", "shots"],
        "params": params.to_dict(),
        "matches": recent,
        "checkpoints": checkpoint_table(list(timelines)),
        "finished_matches": sum(1 for t in timelines if t.get("final")),
    }
