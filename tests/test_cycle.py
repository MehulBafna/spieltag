"""The production cycle, replayed against a simulated clock and a simulated league."""

import io
import json
import shutil
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from replay import ClockFetcher, hours, lake_with_history, utc

from spieltag.contracts import ContractError
from spieltag.cycle import CycleError, run_cycle
from spieltag.evaluate import walk_forward
from spieltag.features import DEFAULT_LEAD, build_features
from spieltag.frozen import freeze_due, frozen_key, frozen_match_ids, load_frozen, parse_time
from spieltag.metrics import score_predictions
from spieltag.models import ClimatologyModel, default_models
from spieltag.pipeline import load_matches
from spieltag.store import LocalStore

LEAGUE = "sim"
FIRST_SEASON = 2016
START = utc(2020, 8, 12, 12)  # two days before the first kickoff of 2020
STOP = utc(2020, 9, 24)  # about six matchdays in


def cycle(store, fetcher, moment, **kwargs):
    fetcher.now = moment
    return run_cycle(
        store, league=LEAGUE, now=moment, fetcher=fetcher, first_season=FIRST_SEASON, **kwargs
    )


@pytest.fixture(scope="module")
def replay(league_matches, tmp_path_factory):
    store, fetcher = lake_with_history(league_matches, tmp_path_factory.mktemp("lake"))
    results = [cycle(store, fetcher, moment) for moment in hours(START, STOP, 12)]
    return store, fetcher, results


@pytest.fixture(scope="module")
def season_2020(league_matches):
    return league_matches[league_matches["season"] == 2020]


def records_of(store):
    records, problems = load_frozen(store, LEAGUE, [2020])
    assert not problems
    return records


def test_every_match_gets_exactly_one_forecast_and_none_are_missed(replay, season_2020):
    store, _, results = replay
    ids = [r["match_id"] for r in records_of(store)]
    played = season_2020[season_2020["kickoff_utc"] <= pd.Timestamp(STOP)]
    assert len(ids) == len(set(ids))
    assert set(played["match_id"]) <= set(ids)
    assert all(not r.missed for r in results)
    assert all(r.ingest_status != "failed" for r in results)


def test_forecasts_are_frozen_after_their_time_has_come_and_before_kickoff(replay):
    store, _, _ = replay
    for record in records_of(store):
        frozen_at = parse_time(record["frozen_at_utc"])
        assert parse_time(record["as_of_utc"]) <= frozen_at < parse_time(record["kickoff_utc"])
        assert record["training"]["trained_until"] <= record["as_of_utc"]


def test_live_forecasts_equal_what_the_backtest_says_they_would_have_been(replay, league_matches):
    """The production cycle and the walk-forward backtest must agree to the last digit."""
    store, _, _ = replay
    backtest = walk_forward(
        build_features(league_matches), default_models(), first_test_season=2020
    ).set_index(["match_id", "model"])
    checked = 0
    for record in records_of(store):
        for model, probabilities in record["models"].items():
            expected = backtest.loc[(record["match_id"], model)]
            for column in ("p_home", "p_draw", "p_away"):
                assert probabilities[column] == pytest.approx(expected[column], abs=1e-9)
            assert record["training"]["matches"] == expected["train_matches"]
            checked += 1
    assert checked > 100


def test_the_scoreboard_matches_scoring_the_backtest_directly(replay, league_matches):
    store, _, results = replay
    live = json.loads(store.get_bytes("site/data/live.json"))
    backtest = walk_forward(
        build_features(league_matches), default_models(), first_test_season=2020
    )
    known = pd.Timestamp(STOP) - timedelta(hours=3)
    done = backtest[backtest["kickoff_utc"] <= known]
    scored = score_predictions(done)

    assert live["matches_scored"] == done["match_id"].nunique() > 40
    assert results[-1].scored == live["matches_scored"]
    for model, own in scored.groupby("model"):
        assert live["models"][model]["rps"] == pytest.approx(own["rps"].mean(), abs=1e-9)
        assert live["models"][model]["matches"] == len(own)


def clone(store, destination):
    shutil.copytree(store.root, destination, dirs_exist_ok=True)
    return LocalStore(destination)


def test_a_second_run_at_the_same_time_changes_nothing(replay, tmp_path):
    store, fetcher, _ = replay
    store = clone(store, tmp_path)
    before = {key: store.get_bytes(key) for key in store.list_keys("predictions/")}
    again = cycle(store, fetcher, STOP)
    assert again.frozen_now == 0
    assert {key: store.get_bytes(key) for key in store.list_keys("predictions/")} == before


def test_a_frozen_forecast_is_never_overwritten_even_if_the_data_changes(
    replay, league_matches, tmp_path
):
    store = clone(replay[0], tmp_path)
    before = {key: store.get_bytes(key) for key in store.list_keys("predictions/")}
    twisted = league_matches.copy()
    twisted["home_goals"] = (twisted["home_goals"] + 3).astype("Int64")  # a different world
    result = run_cycle(
        store, league=LEAGUE, now=STOP, fetcher=ClockFetcher(twisted, STOP), first_season=2016
    )
    assert {key: store.get_bytes(key) for key in store.list_keys("predictions/")} == before
    assert result.frozen_now == 0


def test_the_site_files_are_valid_json_with_the_expected_parts(replay):
    store, _, _ = replay
    text = {
        name: store.get_bytes(f"site/data/{name}.json").decode()
        for name in ("status", "live", "upcoming")
    }
    for body in text.values():
        assert "NaN" not in body and "Infinity" not in body
    status, live, upcoming = (json.loads(text[n]) for n in ("status", "live", "upcoming"))
    assert status["healthy"] is True
    assert status["counts"]["frozen_total"] >= live["matches_scored"] > 0
    assert live["primary_model"] == "elo_ordered_logit"
    assert live["primary_vs_reference"]["verdict"] in {
        "too_early",
        "better",
        "worse",
        "not_distinguishable",
    }
    assert live["trend"][-1]["matches"] == live["matches_scored"]
    assert len(live["recent"]) == 15
    assert upcoming["fixtures"], "there are fixtures ahead"
    assert {f["status"] for f in upcoming["fixtures"]} <= {"frozen", "scheduled"}
    scheduled = [f for f in upcoming["fixtures"] if f["status"] == "scheduled"]
    assert all(f["freezes_at_utc"] for f in scheduled)


def test_a_missed_window_is_reported_and_never_backfilled(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    # The scheduler is down for three days. Saturday 14:00: six matches of matchday 1 are under
    # way or over, with no forecast. Three more kick off later; two of those are already due, so
    # they can still be frozen in time.
    now = utc(2020, 8, 15, 14)
    result = cycle(store, fetcher, now)

    season = league_matches[league_matches["season"] == 2020]
    kicked_off = set(season[season["kickoff_utc"] <= pd.Timestamp(now)]["match_id"])
    frozen = frozen_match_ids(store, LEAGUE, [2020])
    assert len(kicked_off) == 6
    assert set(result.missed) == kicked_off
    assert not frozen & kicked_off  # nothing is ever written after kickoff
    assert result.frozen_now == len(frozen) == 2  # the late ones are still frozen, in time
    assert not result.healthy
    for record in records_of(store):
        assert parse_time(record["frozen_at_utc"]) < parse_time(record["kickoff_utc"])
    status = json.loads(store.get_bytes("site/data/status.json"))
    assert status["healthy"] is False
    assert status["counts"]["missed"] == 6


def test_matches_that_kicked_off_before_go_live_are_not_blamed(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    late_start = utc(2020, 9, 4, 0)  # three matchdays have already been played
    result = cycle(store, fetcher, late_start)
    assert result.missed == []
    assert result.healthy
    assert result.frozen_now > 0


def test_when_the_api_is_down_forecasts_are_still_frozen_from_stored_data(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    fetcher.fail_with = ConnectionError("api.openligadb.de unreachable")

    result = cycle(store, fetcher, utc(2020, 8, 14, 0))

    assert result.ingest_status == "failed"
    assert "unreachable" in result.ingest_error
    assert result.frozen_now > 0  # matchday 1 is due and the stored fixtures are enough
    assert not result.healthy
    status = json.loads(store.get_bytes("site/data/status.json"))
    assert status["ingest"]["status"] == "failed"
    assert status["ingest"]["last_success_utc"] == "2020-08-12T12:00:00Z"
    assert status["healthy"] is False


def test_bad_data_stops_the_ingest_but_not_the_forecasts(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    curated = "curated/matches/league=sim/season=2020/matches.parquet"
    good = store.get_bytes(curated)

    def corrupt(raw):  # a match marked finished without any score
        raw[0]["matchIsFinished"] = True
        raw[0]["matchResults"] = []
        return raw

    fetcher.edit = corrupt
    result = cycle(store, fetcher, utc(2020, 8, 14, 0))

    assert result.ingest_status == "failed"
    assert "finished_matches_have_a_score" in result.ingest_error
    assert store.get_bytes(curated) == good  # the curated table did not change
    assert result.frozen_now > 0


def test_a_failure_while_freezing_is_published_and_reported(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)

    class Broken(ClimatologyModel):
        def fit(self, frame):
            raise RuntimeError("the optimiser exploded")

    result = cycle(store, fetcher, utc(2020, 8, 14, 0), factories={"broken": Broken})

    assert result.freeze_error and "exploded" in result.freeze_error
    assert result.frozen_now == 0
    status = json.loads(store.get_bytes("site/data/status.json"))
    assert status["healthy"] is False
    assert "exploded" in status["freeze_error"]


def test_a_forecast_edited_after_the_fact_is_detected_and_not_scored(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, utc(2020, 8, 14, 0))
    key = store.list_keys("predictions/")[0]
    record = json.loads(store.get_bytes(key))
    record["models"]["elo_ordered_logit"].update(p_home=0.98, p_draw=0.01, p_away=0.01)
    store.put_bytes(key, json.dumps(record).encode())  # someone rewrites history

    result = cycle(store, fetcher, utc(2020, 8, 14, 6))

    assert any("fingerprint" in problem for problem in result.problems)
    assert not result.healthy
    records, problems = load_frozen(store, LEAGUE, [2020])
    assert len(problems) == 1
    assert record["match_id"] not in {r["match_id"] for r in records}


def test_a_forecast_for_a_match_moved_to_before_the_freeze_is_voided(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, utc(2020, 8, 14, 0))
    record = records_of(store)[0]
    frozen_at = parse_time(record["frozen_at_utc"])

    moved = league_matches.copy()
    hit = moved["match_id"] == record["match_id"]
    moved.loc[hit, "kickoff_utc"] = frozen_at - timedelta(hours=30)  # played before the freeze
    late = ClockFetcher(moved, utc(2020, 9, 30))
    result = run_cycle(
        store, league=LEAGUE, now=utc(2020, 9, 30), fetcher=late, first_season=FIRST_SEASON
    )

    assert result.voided == 1
    assert any("kickoff moved" in problem for problem in result.problems)
    assert json.loads(store.get_bytes("site/data/status.json"))["counts"]["voided"] == 1


def test_an_empty_store_cannot_run(tmp_path):
    fetcher = ClockFetcher(pd.DataFrame(), START)
    fetcher.fail_with = ConnectionError("down")
    with pytest.raises(CycleError):
        run_cycle(LocalStore(tmp_path), league=LEAGUE, now=START, fetcher=fetcher)


def test_the_go_live_time_is_written_once(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    cycle(store, fetcher, utc(2020, 8, 13, 0))
    go_live = json.loads(store.get_bytes("meta/league=sim/go_live.json"))
    assert go_live["go_live_utc"] == "2020-08-12T12:00:00Z"


def test_the_clock_must_know_its_time_zone(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        run_cycle(store, league=LEAGUE, now=START.replace(tzinfo=None), fetcher=fetcher)


def test_probabilities_in_every_record_are_distributions(replay):
    store, _, _ = replay
    for record in records_of(store):
        for probabilities in record["models"].values():
            values = np.array(list(probabilities.values()))
            assert (values > 0).all() and values.sum() == pytest.approx(1.0)


def test_contract_errors_from_stored_data_are_not_swallowed(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    # The same matches stored under two seasons is corruption, not an ingest hiccup.
    buffer = io.BytesIO()
    league_matches[league_matches["season"] == 2019].to_parquet(buffer, index=False)
    store.put_bytes("curated/matches/league=sim/season=2018/matches.parquet", buffer.getvalue())
    with pytest.raises(ContractError):
        cycle(store, fetcher, utc(2020, 8, 13, 0))


def test_frozen_keys_are_laid_out_by_league_season_and_match():
    assert (
        frozen_key("bl1", 2026, 70001) == "predictions/league=bl1/season=2026/match_id=70001.json"
    )


def test_two_overlapping_runs_cannot_both_write_a_forecast(league_matches, tmp_path):
    """Even a run that does not know a forecast exists must not replace it."""
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    matches, _ = load_matches(store, LEAGUE, range(FIRST_SEASON, 2021))
    features = build_features(matches)
    now = utc(2020, 8, 14, 0)

    def freeze(version):
        return freeze_due(
            store,
            matches,
            features,
            default_models(),
            league=LEAGUE,
            now=now,
            lead=DEFAULT_LEAD,
            min_train_matches=600,
            code_version=version,
            already_frozen=set(),  # a stale view, as a second concurrent run would have
        )

    first, second = freeze("first"), freeze("second")

    assert len(first.frozen) == 1 and not first.lost_race
    assert second.frozen == [] and second.lost_race == first.frozen
    record = json.loads(store.get_bytes(store.list_keys("predictions/")[0]))
    assert record["code_version"] == "first"


def test_a_miss_raises_the_alarm_for_three_days_and_then_stays_on_record(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path)
    cycle(store, fetcher, START)
    during = cycle(store, fetcher, utc(2020, 8, 15, 14))  # six matches kicked off unseen
    assert len(during.missed) == 6 and during.missed_total == 6

    # The last matchday-1 match kicks off on Sunday 16:00, before the next run: a seventh miss.
    # More than three days later the alarm clears, but all seven stay on record.
    later = cycle(store, fetcher, utc(2020, 8, 20, 0))
    assert later.missed == [] and later.missed_total == 7
    assert later.healthy
    status = json.loads(store.get_bytes("site/data/status.json"))
    assert status["counts"]["missed"] == 0 and status["counts"]["missed_total"] == 7
