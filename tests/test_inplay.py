import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from replay import utc

from spieltag import inplay
from spieltag.frozen import fingerprint, frozen_key
from spieltag.inplay import (
    InplayParams,
    StateFactors,
    add_point,
    checkpoint_table,
    current_score,
    estimate_minute,
    final_score,
    finalise,
    fit_rates,
    goal_events,
    inplay_key,
    load_params,
    new_timeline,
    outcome_probabilities,
    params_key,
    remaining_goal_distribution,
    run_inplay_tick,
)
from spieltag.inplay_fit import (
    MatchGoals,
    estimate_first_half_share,
    estimate_state_factors,
    evaluate_variants,
    fit_params,
    paired_over_matches,
    shrink,
    slice_index,
)
from spieltag.store import LocalStore

KICKOFF = utc(2026, 10, 10, 13) + timedelta(minutes=30)
SEALED = {"p_home": 0.5, "p_draw": 0.25, "p_away": 0.25}


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def sealed_record(match_id=7, probs=SEALED, kickoff=KICKOFF):
    record = {
        "schema_version": 1,
        "league": "bl1",
        "season": 2026,
        "matchday": 5,
        "match_id": match_id,
        "home_team": "Home FC",
        "away_team": "Away FC",
        "kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_utc": (kickoff - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frozen_at_utc": (kickoff - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lead_hours": 24.0,
        "primary_model": "elo_ordered_logit",
        "models": {"elo_ordered_logit": dict(probs)},
        "code_version": "test",
    }
    record["fingerprint"] = fingerprint(record)
    return record


def write_sealed(store, record):
    body = json.dumps(record, indent=2, sort_keys=True).encode()
    store.put_bytes_if_absent(frozen_key("bl1", 2026, record["match_id"]), body)
    return body


def goal(minute, home, away, goal_id):
    return {
        "goalID": goal_id,
        "matchMinute": minute,
        "scoreTeam1": home,
        "scoreTeam2": away,
        "isOwnGoal": False,
    }


def raw_match(match_id=7, *, goals=(), finished=False, final=None):
    results = []
    if final:
        results = [
            {"resultTypeID": 1, "pointsTeam1": 0, "pointsTeam2": 0},
            {"resultTypeID": 2, "pointsTeam1": final[0], "pointsTeam2": final[1]},
        ]
    return {
        "matchID": match_id,
        "matchIsFinished": finished,
        "goals": list(goals),
        "matchResults": results,
        "lastUpdateDateTime": "2026-10-10T15:00:00",
    }


class Feed:
    def __init__(self, matches):
        self.matches = matches
        self.error = None
        self.calls = 0

    def __call__(self, league, season):
        self.calls += 1
        if self.error:
            raise self.error
        return self.matches


# ---------------------------------------------------------------------------------------------
# the maths
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "triple",
    [
        (0.5, 0.25, 0.25),
        (0.72, 0.17, 0.11),
        (0.16, 0.21, 0.63),
        (0.38, 0.28, 0.34),
        (0.9, 0.07, 0.03),
    ],
)
def test_fitted_rates_reproduce_the_sealed_probabilities(triple):
    lam_home, lam_away, gap = fit_rates(*triple)

    probabilities, _, _ = outcome_probabilities(lam_home, lam_away, 0, (0, 0))

    assert gap < 0.005
    assert probabilities == pytest.approx(triple, abs=0.005)


def test_a_stronger_home_side_gets_more_expected_goals():
    strong = fit_rates(0.7, 0.17, 0.13)
    weak = fit_rates(0.25, 0.27, 0.48)
    assert strong[0] > strong[1] and weak[0] < weak[1]
    assert strong[0] > weak[0] and strong[1] < weak[1]


def test_the_live_line_starts_at_the_sealed_forecast():
    lam_home, lam_away, _ = fit_rates(0.5, 0.25, 0.25)
    probabilities, exp_home, exp_away = outcome_probabilities(lam_home, lam_away, 0, (0, 0))
    assert probabilities == pytest.approx([0.5, 0.25, 0.25], abs=0.005)
    assert exp_home == pytest.approx(lam_home, abs=1e-3)
    assert exp_away == pytest.approx(lam_away, abs=1e-3)


def test_probabilities_always_form_a_distribution():
    lam_home, lam_away, _ = fit_rates(0.5, 0.25, 0.25)
    for minute in (0, 1, 44, 45, 46, 89, 90, 95):
        for score in ((0, 0), (1, 0), (0, 3), (2, 2), (5, 1)):
            for params in (InplayParams(), InplayParams(factors=StateFactors(1.3, 1.2, 0.9, 0.8))):
                p, _, _ = outcome_probabilities(lam_home, lam_away, minute, score, params)
                assert p.sum() == pytest.approx(1.0)
                assert (p >= 0).all()


def test_a_lead_becomes_safer_as_the_clock_runs():
    lam_home, lam_away, _ = fit_rates(0.4, 0.28, 0.32)
    wins = [
        outcome_probabilities(lam_home, lam_away, minute, (1, 0))[0][0]
        for minute in (10, 30, 50, 70, 85)
    ]
    assert wins == sorted(wins)
    assert wins[-1] > 0.9


def test_a_goal_moves_the_line_the_right_way():
    lam_home, lam_away, _ = fit_rates(0.5, 0.25, 0.25)
    level = outcome_probabilities(lam_home, lam_away, 30, (0, 0))[0]
    home_up = outcome_probabilities(lam_home, lam_away, 30, (1, 0))[0]
    away_up = outcome_probabilities(lam_home, lam_away, 30, (0, 1))[0]
    assert home_up[0] > level[0] > away_up[0]
    assert home_up[2] < level[2] < away_up[2]


def test_a_big_lead_late_is_decided_and_the_final_whistle_is_certain():
    lam_home, lam_away, _ = fit_rates(0.5, 0.25, 0.25)
    assert outcome_probabilities(lam_home, lam_away, 80, (3, 0))[0][0] > 0.999
    certain = outcome_probabilities(lam_home, lam_away, 90, (0, 2))[0]
    assert certain[2] > 0.99


def test_the_minute_by_minute_calculation_matches_the_closed_form():
    lam_home, lam_away, _ = fit_rates(0.5, 0.25, 0.25)
    neutral = remaining_goal_distribution(lam_home, lam_away, 30, 0, InplayParams())
    # a factor this close to 1 forces the stepping code path
    stepped = remaining_goal_distribution(
        lam_home, lam_away, 30, 0, InplayParams(factors=StateFactors(trail2=1.0000001))
    )
    assert np.abs(neutral - stepped).max() < 1e-4


def test_trailing_teams_that_attack_more_come_back_more_often():
    lam_home, lam_away, _ = fit_rates(0.5, 0.25, 0.25)
    neutral = outcome_probabilities(lam_home, lam_away, 45, (0, 1))[0]
    attacking = outcome_probabilities(
        lam_home, lam_away, 45, (0, 1), InplayParams(factors=StateFactors(1.4, 1.3, 0.9, 0.8))
    )[0]
    assert attacking[0] > neutral[0] and attacking[1] > neutral[1]


# ---------------------------------------------------------------------------------------------
# what the feed gives us
# ---------------------------------------------------------------------------------------------


def test_goals_are_read_in_the_order_they_were_scored_even_if_listed_out_of_order():
    match = raw_match(goals=[goal(70, 2, 1, 3), goal(10, 1, 0, 1), goal(55, 1, 1, 2)])
    assert goal_events(match) == [(10, 1, 0), (55, 1, 1), (70, 2, 1)]
    assert current_score(match) == (2, 1)


def test_a_goal_without_a_minute_is_kept_and_one_without_a_score_is_dropped():
    match = raw_match(goals=[goal(None, 1, 0, 1), {"goalID": 2, "matchMinute": 20}])
    assert goal_events(match) == [(0, 1, 0)]


def test_a_match_with_no_goals_is_nil_nil():
    assert current_score(raw_match()) == (0, 0)


def test_the_final_score_is_the_result_after_90_minutes():
    assert final_score(raw_match(final=(2, 1), finished=True)) == (2, 1)
    assert final_score(raw_match()) is None


@pytest.mark.parametrize(
    "elapsed, expected",
    [
        (-5, (0, "before_kickoff")),
        (0, (0, "first_half")),
        (20, (20, "first_half")),
        (46, (45, "first_half")),
        (50, (45, "half_time")),
        (61, (45, "half_time")),
        (62, (45, "second_half")),
        (92, (75, "second_half")),
        (110, (93, "second_half")),
    ],
)
def test_the_match_minute_is_estimated_from_the_kickoff_time(elapsed, expected):
    assert estimate_minute(pd.Timestamp(KICKOFF), KICKOFF + timedelta(minutes=elapsed)) == expected


# ---------------------------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------------------------


def test_missing_parameters_mean_neutral_defaults(tmp_path):
    params = load_params(LocalStore(tmp_path), "bl1")
    assert params.factors.neutral and params.first_half_share == 0.45


def test_fitted_parameters_are_loaded_and_implausible_ones_are_refused(tmp_path, capsys):
    store = LocalStore(tmp_path)
    good = InplayParams(0.43, StateFactors(1.2, 1.1, 0.95, 0.9), "test")
    store.put_bytes(params_key("bl1"), json.dumps(good.to_dict()).encode())
    assert load_params(store, "bl1") == good

    store.put_bytes(params_key("bl1"), json.dumps({"factors": {"trail1": 9.0}}).encode())
    fallback = load_params(store, "bl1")
    assert fallback.factors.neutral and "invalid" in fallback.source
    assert "inplay_params_invalid" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------
# a match's timeline
# ---------------------------------------------------------------------------------------------


def test_a_timeline_records_the_sealed_forecast_and_the_estimates(tmp_path):
    timeline = new_timeline(sealed_record(), InplayParams())
    add_point(
        timeline,
        now=KICKOFF + timedelta(minutes=30),
        score=(1, 0),
        minute=30,
        phase="first_half",
        feed_updated=None,
        params=InplayParams(),
    )
    point = timeline["points"][0]
    assert timeline["sealed"] == SEALED
    assert point["p_home"] > 0.5 and point["exp_final_home"] > 1.0
    assert sum(point[k] for k in ("p_home", "p_draw", "p_away")) == pytest.approx(1.0, abs=1e-4)


def test_two_points_for_the_same_time_keep_only_the_latest():
    timeline = new_timeline(sealed_record(), InplayParams())
    when = KICKOFF + timedelta(minutes=10)
    for score in ((0, 0), (1, 0)):
        add_point(
            timeline,
            now=when,
            score=score,
            minute=10,
            phase="first_half",
            feed_updated=None,
            params=InplayParams(),
        )
    assert len(timeline["points"]) == 1 and timeline["points"][0]["home"] == 1


# ---------------------------------------------------------------------------------------------
# the scheduled tick
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "lake")


def test_nothing_happens_before_kickoff(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match()])

    result = run_inplay_tick(store, league="bl1", now=KICKOFF - timedelta(hours=1), fetcher=feed)

    assert result.candidates == 0 and not result.published and feed.calls == 0
    with pytest.raises(FileNotFoundError):
        store.get_bytes("site/data/inplay.json")


def test_a_match_in_play_gets_a_timeline_and_a_published_summary(store):
    sealed_bytes = write_sealed(store, sealed_record())
    feed = Feed([raw_match(goals=[goal(12, 1, 0, 1)])])

    result = run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=20), fetcher=feed)

    assert (result.candidates, result.live, result.published) == (1, 1, True)
    timeline = json.loads(store.get_bytes(inplay_key("bl1", 2026, 7)))
    assert timeline["points"][-1]["home"] == 1 and timeline["points"][-1]["minute"] == 20
    summary = json.loads(store.get_bytes("site/data/inplay.json"))
    assert summary["matches"][0]["match_id"] == 7
    assert "red cards" in summary["not_used"] and summary["minute_is_estimated"]
    # the sealed forecast is exactly what it was
    assert store.get_bytes(frozen_key("bl1", 2026, 7)) == sealed_bytes


def test_the_estimate_follows_the_match_over_several_ticks(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match()])
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=5), fetcher=feed)
    feed.matches = [raw_match(goals=[goal(30, 0, 1, 1)])]
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=35), fetcher=feed)

    points = json.loads(store.get_bytes(inplay_key("bl1", 2026, 7)))["points"]

    assert [p["minute"] for p in points] == [5, 35]
    assert points[1]["p_away"] > points[0]["p_away"]


def test_a_feed_failure_keeps_the_last_estimate_and_is_counted(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match()])
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=10), fetcher=feed)
    before = store.get_bytes(inplay_key("bl1", 2026, 7))
    feed.error = ConnectionError("offline")

    result = run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=15), fetcher=feed)

    assert "offline" in result.feed_error
    assert result.metrics()["InplayFeedFailed"] == (1, "Count")
    assert store.get_bytes(inplay_key("bl1", 2026, 7)) == before


def test_a_finished_match_is_closed_with_the_result_and_not_touched_again(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match(goals=[goal(10, 1, 0, 1), goal(80, 2, 0, 2)])])
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=95), fetcher=feed)
    feed.matches = [
        raw_match(goals=[goal(10, 1, 0, 1), goal(80, 2, 0, 2)], finished=True, final=(2, 0))
    ]

    result = run_inplay_tick(
        store, league="bl1", now=KICKOFF + timedelta(minutes=118), fetcher=feed
    )

    timeline = json.loads(store.get_bytes(inplay_key("bl1", 2026, 7)))
    assert result.finished_now == 1
    assert timeline["final"]["outcome"] == 0 and timeline["points"][-1]["p_home"] == 1.0
    assert timeline["points"][-1]["phase"] == "full_time"

    snapshot = store.get_bytes(inplay_key("bl1", 2026, 7))
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=123), fetcher=feed)
    assert store.get_bytes(inplay_key("bl1", 2026, 7)) == snapshot


def test_a_closed_match_is_not_fetched_again_in_the_hours_after_full_time(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match(goals=[], finished=True, final=(0, 0))])
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=118), fetcher=feed)
    calls = feed.calls

    result = run_inplay_tick(
        store, league="bl1", now=KICKOFF + timedelta(minutes=123), fetcher=feed
    )

    assert result.candidates == 0 and feed.calls == calls


def test_an_idle_tick_long_after_the_match_does_no_work(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match(goals=[], finished=True, final=(0, 0))])
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=118), fetcher=feed)
    calls = feed.calls

    result = run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(hours=9), fetcher=feed)

    assert result.candidates == 0 and feed.calls == calls


def test_a_match_the_feed_did_not_finish_is_still_checked_later(store):
    write_sealed(store, sealed_record())
    feed = Feed([raw_match(goals=[goal(10, 1, 0, 1)])])
    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=100), fetcher=feed)

    # five hours on the feed finally says the match is over
    feed.matches = [raw_match(goals=[goal(10, 1, 0, 1)], finished=True, final=(1, 0))]
    result = run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(hours=5), fetcher=feed)

    assert result.candidates == 1 and result.finished_now == 1


def test_a_tampered_sealed_record_is_reported_and_not_used(store):
    record = sealed_record()
    record["models"]["elo_ordered_logit"]["p_home"] = 0.9  # edited after sealing
    store.put_bytes_if_absent(frozen_key("bl1", 2026, 7), json.dumps(record).encode())

    result = run_inplay_tick(
        store, league="bl1", now=KICKOFF + timedelta(minutes=20), fetcher=Feed([raw_match()])
    )

    assert result.candidates == 0 and result.problems


def test_only_sealed_matches_are_followed(store):
    write_sealed(store, sealed_record(match_id=7))
    feed = Feed([raw_match(7), raw_match(8, goals=[goal(5, 1, 0, 1)])])

    run_inplay_tick(store, league="bl1", now=KICKOFF + timedelta(minutes=20), fetcher=feed)

    assert store.list_keys("inplay/league=bl1/") == [inplay_key("bl1", 2026, 7)]


# ---------------------------------------------------------------------------------------------
# scoring the live line against the sealed line
# ---------------------------------------------------------------------------------------------


def finished_timeline(outcome, live_by_minute, sealed=SEALED):
    timeline = new_timeline(sealed_record(probs=sealed), InplayParams())
    for minute, probs in live_by_minute.items():
        timeline["points"].append(
            {
                "t": f"2026-10-10T15:{minute:02d}:00Z",
                "minute": minute,
                "phase": "first_half",
                "home": 0,
                "away": 0,
                "p_home": probs[0],
                "p_draw": probs[1],
                "p_away": probs[2],
            }
        )
    finalise(timeline, (1, 0) if outcome == 0 else (0, 1), KICKOFF + timedelta(hours=2))
    return timeline


def test_the_checkpoint_table_scores_live_against_sealed_at_each_minute():
    good = finished_timeline(0, {60: (0.9, 0.07, 0.03)})
    bad = finished_timeline(0, {60: (0.2, 0.2, 0.6)})

    rows = {r["minute"]: r for r in checkpoint_table([good, bad])}

    assert rows[60]["matches"] == 2 and rows[60]["live_better"] == 1
    assert rows[30]["matches"] == 0 and "rps_live" not in rows[30]
    assert rows[60]["rps_sealed"] == pytest.approx(0.5 * ((0.5 - 1) ** 2 + (0.75 - 1) ** 2))


def test_unfinished_matches_and_far_away_estimates_do_not_count():
    unfinished = finished_timeline(0, {60: (0.9, 0.07, 0.03)})
    unfinished["final"] = None
    far = finished_timeline(0, {50: (0.9, 0.07, 0.03)})  # 10 minutes from the 60 checkpoint

    rows = {r["minute"]: r for r in checkpoint_table([unfinished, far])}

    assert rows[60]["matches"] == 0


# ---------------------------------------------------------------------------------------------
# estimating the parameters from goal times
# ---------------------------------------------------------------------------------------------


def simulate_matches(count, params, seed):
    """Matches whose goals follow the process with the given parameters. Invented data."""
    rng = np.random.default_rng(seed)
    weights = inplay.minute_weights(params.first_half_share)
    matches = []
    for match_id in range(count):
        lam_home, lam_away = rng.uniform(0.9, 2.4), rng.uniform(0.6, 1.9)
        probabilities = inplay._match_outcome_probabilities(lam_home, lam_away)
        events, score = [], [0, 0]
        for minute in range(1, 91):
            diff = score[0] - score[1]
            mu_home = lam_home * weights[minute - 1] * float(params.factors.lookup(np.array(diff)))
            mu_away = lam_away * weights[minute - 1] * float(params.factors.lookup(np.array(-diff)))
            for _ in range(rng.poisson(mu_home)):
                score[0] += 1
                events.append((minute, score[0], score[1]))
            for _ in range(rng.poisson(mu_away)):
                score[1] += 1
                events.append((minute, score[0], score[1]))
        matches.append(
            MatchGoals(
                match_id,
                2020 + match_id % 4,
                tuple(probabilities),
                lam_home,
                lam_away,
                events,
                (score[0], score[1]),
            )
        )
    return matches


@pytest.mark.parametrize(
    "minute, expected", [(0, 0), (1, 0), (5, 0), (6, 1), (45, 8), (46, 9), (90, 17), (95, 17)]
)
def test_goals_fall_into_five_minute_slices(minute, expected):
    assert slice_index(minute) == expected


def test_the_first_half_share_is_recovered_from_goal_times():
    truth = InplayParams(first_half_share=0.40)
    matches = simulate_matches(2500, truth, seed=1)
    share, goals = estimate_first_half_share(matches)
    assert goals > 5000 and share == pytest.approx(0.40, abs=0.02)


def test_a_planted_state_effect_is_recovered_with_its_interval():
    truth = InplayParams(factors=StateFactors(trail2=1.35, trail1=1.2, lead1=0.9, lead2=0.8))
    matches = simulate_matches(4000, truth, seed=2)

    table = estimate_state_factors(matches, 0.45)

    for name, value in (("trail1", 1.2), ("lead1", 0.9), ("trail2", 1.35), ("lead2", 0.8)):
        entry = table[name]
        assert entry["low"] <= value <= entry["high"], (name, entry)
    assert table["level"]["relative"] == 1.0
    assert table["level"]["goal_scale"] == pytest.approx(1.0, abs=0.05)


def test_with_no_state_effect_the_factors_stay_close_to_one():
    matches = simulate_matches(3000, InplayParams(), seed=3)
    table = estimate_state_factors(matches, 0.45)
    fitted = shrink(table)
    for name in ("trail2", "trail1", "lead1", "lead2"):
        assert table[name]["low"] <= 1.0 <= table[name]["high"], (name, table[name])
        assert getattr(fitted, name) == pytest.approx(1.0, abs=0.08)


def test_factors_resting_on_few_goals_are_pulled_towards_one():
    table = {
        "trail2": {"goals": 10.0, "relative": 2.0},
        "trail1": {"goals": 5000.0, "relative": 2.0},
        "lead1": {"goals": 0.0, "relative": float("nan")},
        "lead2": {"goals": 50.0, "relative": 0.5},
    }
    fitted = shrink(table)
    assert 1.0 < fitted.trail2 < 1.3  # barely moved from 1
    assert fitted.trail1 == pytest.approx(2.0, rel=0.02)  # trusted
    assert fitted.lead1 == 1.0  # nothing to go on
    assert fitted.lead2 == pytest.approx(0.5**0.5)  # halfway


def test_fitted_parameters_beat_neutral_on_held_out_matches_when_the_effect_is_real():
    truth = InplayParams(factors=StateFactors(trail2=1.4, trail1=1.3, lead1=0.85, lead2=0.7))
    train = simulate_matches(3500, truth, seed=4)
    test = simulate_matches(600, truth, seed=5)
    fitted, _ = fit_params(train, "test")

    scores = evaluate_variants(test, {"neutral": InplayParams(), "fitted": fitted})
    gap = paired_over_matches(scores, "fitted", "neutral")

    assert gap["mean"] < 0 and gap["high"] < 0


def test_fitted_parameters_do_not_pretend_to_help_when_there_is_no_effect():
    train = simulate_matches(3000, InplayParams(), seed=6)
    test = simulate_matches(600, InplayParams(), seed=7)
    fitted, _ = fit_params(train, "test")

    scores = evaluate_variants(test, {"neutral": InplayParams(), "fitted": fitted})
    gap = paired_over_matches(scores, "fitted", "neutral")

    assert not gap["high"] < 0


def test_the_live_line_beats_the_sealed_line_once_goals_are_known():
    matches = simulate_matches(500, InplayParams(), seed=8)
    scores = evaluate_variants(matches, {"live": InplayParams()})
    by_minute = scores.pivot_table(index="minute", columns="variant", values="rps")
    assert (by_minute["live"] < by_minute["sealed"]).all()
    gains = 1 - by_minute["live"] / by_minute["sealed"]
    assert gains.is_monotonic_increasing  # the later the minute, the more the score tells us


def test_the_live_probabilities_are_calibrated_on_simulated_matches():
    matches = simulate_matches(1500, InplayParams(), seed=9)
    predicted, happened = [], []
    for match in matches:
        probabilities, _, _ = outcome_probabilities(
            match.lam_home, match.lam_away, 60, match.score_at(60)
        )
        predicted.append(probabilities[0])
        happened.append(match.outcome == 0)
    predicted, happened = np.array(predicted), np.array(happened)
    for low, high in ((0, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 1.01)):
        mask = (predicted >= low) & (predicted < high)
        if mask.sum() >= 50:
            assert happened[mask].mean() == pytest.approx(predicted[mask].mean(), abs=0.07)


# ---------------------------------------------------------------------------------------------
# the Lambda entry point and the fitting command
# ---------------------------------------------------------------------------------------------


def test_the_handler_routes_the_five_minute_schedule_to_the_live_task(monkeypatch, capsys):
    from spieltag import handler

    seen = {}

    def fake_tick(store, *, league, now):
        seen["league"] = league
        return inplay.TickResult(now=inplay.iso(now), candidates=1, live=1)

    monkeypatch.setenv("SPIELTAG_BUCKET", "unused")
    monkeypatch.setenv("SPIELTAG_LEAGUE", "sim")
    monkeypatch.setattr(handler, "S3Store", lambda bucket: object())
    monkeypatch.setattr(handler, "run_inplay_tick", fake_tick)

    summary = handler.lambda_handler({"source": "schedule", "task": "inplay"}, None)

    assert seen["league"] == "sim" and summary["live"] == 1
    json.dumps(summary)
    out = capsys.readouterr().out
    assert '"InplayLive": 1' in out and '"InplayFeedFailed": 0' in out


def test_an_idle_tick_emits_only_the_heartbeat_metric():
    idle = inplay.TickResult(now="2026-10-10T12:00:00Z")
    assert set(idle.metrics()) == {"InplayTickCompleted"}


def lake_with_goal_history(tmp_path, matches):
    """Raw snapshots with goals, and the walk-forward predictions, as the CLI expects them."""
    import io

    store = LocalStore(tmp_path / "lake")
    by_season = {}
    rows = []
    for match in matches:
        goals = [goal(m, h, a, i + 1) for i, (m, h, a) in enumerate(match.events)]
        by_season.setdefault(match.season, []).append(
            raw_match(match.match_id, goals=goals, finished=True, final=match.final)
        )
        rows.append(
            {
                "match_id": match.match_id,
                "season": match.season,
                "model": "elo_ordered_logit",
                "p_home": match.sealed[0],
                "p_draw": match.sealed[1],
                "p_away": match.sealed[2],
            }
        )
    for season, raw in by_season.items():
        store.put_bytes(
            f"raw/openligadb/bl1/{season}/20260101T000000Z.json", json.dumps(raw).encode()
        )
    buffer = io.BytesIO()
    pd.DataFrame(rows).to_parquet(buffer, index=False)
    store.put_bytes("evaluation/bl1/predictions.parquet", buffer.getvalue())
    return store


def test_the_fit_command_writes_parameters_only_when_they_win_on_held_out_seasons(tmp_path, capsys):
    from spieltag import inplay_fit_cli

    # a much stronger effect than football shows, so that it is detectable in a small held-out set
    truth = InplayParams(factors=StateFactors(trail2=2.2, trail1=1.8, lead1=0.6, lead2=0.4))
    store = lake_with_goal_history(tmp_path, simulate_matches(2400, truth, seed=10))

    code = inplay_fit_cli.main(["--out", str(store.root), "--write", "--held-out-seasons", "1"])

    out = capsys.readouterr().out
    assert code == 0 and "Written:" in out and "better" in out
    written = load_params(store, "bl1")
    assert written.factors.trail1 > 1.3 and written.factors.lead2 < 0.6


def test_the_fit_command_refuses_to_write_when_nothing_is_gained(tmp_path, capsys):
    from spieltag import inplay_fit_cli

    store = lake_with_goal_history(tmp_path, simulate_matches(2400, InplayParams(), seed=11))

    code = inplay_fit_cli.main(["--out", str(store.root), "--write", "--held-out-seasons", "1"])

    assert code == 1 and "Not written" in capsys.readouterr().out
    assert load_params(store, "bl1").factors.neutral


def test_the_fit_command_skips_matches_whose_goal_list_is_incomplete(tmp_path, capsys):
    from spieltag import inplay_fit_cli

    matches = simulate_matches(400, InplayParams(), seed=12)
    store = lake_with_goal_history(tmp_path, matches)
    victim = next(m for m in matches if m.events)
    key = f"raw/openligadb/bl1/{victim.season}/20260101T000000Z.json"
    raw = json.loads(store.get_bytes(key))
    for item in raw:
        if item["matchID"] == victim.match_id:
            item["goals"] = []  # the feed lost the goals
    store.put_bytes(key, json.dumps(raw).encode())

    inplay_fit_cli.main(["--out", str(store.root), "--held-out-seasons", "1"])

    assert "'goals_incomplete': 1" in capsys.readouterr().out


def test_the_fit_command_needs_the_backtest_predictions(tmp_path, capsys):
    from spieltag import inplay_fit_cli

    assert inplay_fit_cli.main(["--out", str(tmp_path)]) == 2
    assert "spieltag-evaluate" in capsys.readouterr().out
