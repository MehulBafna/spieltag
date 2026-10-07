from datetime import date

import pandas as pd
import pytest
import requests

from spieltag import ingest
from spieltag.ingest import OpenLigaError, current_season, fetch_season_raw, parse_matches


def test_parse_returns_one_row_per_match_with_expected_columns(raw_sample):
    frame = parse_matches(raw_sample)
    assert list(frame.columns) == ingest.MATCH_COLUMNS
    assert len(frame) == 4
    assert frame["kickoff_utc"].dt.tz is not None


def test_final_score_is_picked_by_result_type_not_by_position(raw_sample):
    frame = parse_matches(raw_sample).set_index("match_id")
    # match 1001 lists full time first, match 1002 lists half time first
    assert (frame.loc[1001, "home_goals"], frame.loc[1001, "away_goals"]) == (2, 1)
    assert (frame.loc[1002, "home_goals"], frame.loc[1002, "away_goals"]) == (0, 0)


def test_unfinished_matches_have_no_score(raw_sample):
    frame = parse_matches(raw_sample).set_index("match_id")
    assert not frame.loc[1003, "is_finished"]
    assert pd.isna(frame.loc[1003, "home_goals"])
    assert pd.isna(frame.loc[1003, "away_goals"])


def test_field_names_are_matched_ignoring_case(raw_sample):
    shouty = [{key.upper(): value for key, value in match.items()} for match in raw_sample]
    assert len(parse_matches(shouty)) == 4


def test_missing_field_gives_a_readable_error(raw_sample):
    del raw_sample[0]["team1"]
    with pytest.raises(OpenLigaError, match="team1"):
        parse_matches(raw_sample)


def test_empty_answer_gives_empty_frame_with_columns():
    frame = parse_matches([], league_season=2025)
    assert frame.empty
    assert list(frame.columns) == ingest.MATCH_COLUMNS


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda seconds: None)


def test_fetch_builds_the_documented_url(raw_sample):
    session = FakeSession(FakeResponse(payload=raw_sample))
    assert fetch_season_raw("bl1", 2025, session=session) == raw_sample
    assert session.calls == ["https://api.openligadb.de/getmatchdata/bl1/2025"]


def test_fetch_retries_network_errors_then_succeeds(raw_sample):
    session = FakeSession(
        requests.ConnectionError("down"), FakeResponse(503), FakeResponse(payload=raw_sample)
    )
    assert fetch_season_raw("bl1", 2025, session=session) == raw_sample
    assert len(session.calls) == 3


def test_fetch_gives_up_after_the_last_retry():
    session = FakeSession(*[requests.Timeout("slow")] * 3)
    with pytest.raises(OpenLigaError, match="giving up"):
        fetch_season_raw("bl1", 2025, session=session)


def test_fetch_does_not_retry_client_errors():
    session = FakeSession(FakeResponse(404))
    with pytest.raises(requests.HTTPError):
        fetch_season_raw("bl1", 1800, session=session)
    assert len(session.calls) == 1


def test_fetch_rejects_a_non_list_answer():
    session = FakeSession(FakeResponse(payload={"message": "oops"}))
    with pytest.raises(OpenLigaError, match="expected a list"):
        fetch_season_raw("bl1", 2025, session=session)


def test_fetch_rejects_an_answer_that_is_not_json():
    session = FakeSession(FakeResponse(payload=ValueError("not json")))
    with pytest.raises(OpenLigaError, match="did not return JSON"):
        fetch_season_raw("bl1", 2025, session=session)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2025, 8, 22), 2025),
        (date(2025, 12, 31), 2025),
        (date(2026, 5, 16), 2025),
        (date(2026, 7, 1), 2026),
    ],
)
def test_current_season_rolls_over_in_july(today, expected):
    assert current_season(today) == expected
