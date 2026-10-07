import json
from datetime import timedelta
from functools import partial

import pytest
from replay import lake_with_history, utc
from test_pipeline import FakeS3Client

from spieltag import handler, run_cli
from spieltag.cycle import run_cycle
from spieltag.models import ClimatologyModel
from spieltag.observability import log_event, metric_line
from spieltag.store import LocalStore, S3Store

NOW = utc(2020, 8, 14, 0)  # matchday 1 has one match due


@pytest.fixture
def lake(league_matches, tmp_path):
    store, fetcher = lake_with_history(league_matches, tmp_path / "lake")
    fetcher.now = NOW
    return store, fetcher


@pytest.fixture
def lambda_env(monkeypatch, lake):
    store, fetcher = lake
    monkeypatch.setenv("SPIELTAG_BUCKET", "unused")
    monkeypatch.setenv("SPIELTAG_LEAGUE", "sim")
    monkeypatch.setenv("SPIELTAG_CODE_VERSION", "abc1234")
    monkeypatch.setattr(handler, "S3Store", lambda bucket: store)
    monkeypatch.setattr(handler, "run_cycle", partial(run_cycle, fetcher=fetcher, now=NOW))
    return store


def emitted_metrics(capsys):
    lines = [line for line in capsys.readouterr().out.splitlines() if "_aws" in line]
    assert len(lines) == 1
    return json.loads(lines[0])


def test_the_handler_runs_a_cycle_reports_metrics_and_returns_a_summary(lambda_env, capsys):
    summary = handler.lambda_handler({}, None)

    assert summary["frozen_now"] == 1
    assert summary["code_version"] == "abc1234"
    json.dumps(summary)  # the return value must be plain JSON for Lambda
    metrics = emitted_metrics(capsys)
    assert metrics["League"] == "sim"
    assert metrics["CycleCompleted"] == 1 and metrics["FrozenThisRun"] == 1
    assert metrics["MissedFreezes"] == 0 and metrics["IngestFailed"] == 0


def test_a_freeze_failure_raises_so_lambda_counts_an_error(lambda_env, monkeypatch, capsys):
    class Broken(ClimatologyModel):
        def fit(self, frame):
            raise RuntimeError("no fit today")

    fetcher_run = handler.run_cycle
    monkeypatch.setattr(handler, "run_cycle", partial(fetcher_run, factories={"b": Broken}))

    with pytest.raises(handler.CycleFailed, match="no fit today"):
        handler.lambda_handler({}, None)

    assert emitted_metrics(capsys)["CycleCompleted"] == 0


def test_a_crash_is_logged_and_counted_then_raised(monkeypatch, capsys):
    monkeypatch.setenv("SPIELTAG_BUCKET", "unused")
    monkeypatch.setenv("SPIELTAG_LEAGUE", "sim")
    monkeypatch.setattr(handler, "S3Store", lambda bucket: object())

    def explode(*args, **kwargs):
        raise OSError("bucket gone")

    monkeypatch.setattr(handler, "run_cycle", explode)
    with pytest.raises(OSError):
        handler.lambda_handler({}, None)

    out = capsys.readouterr().out
    assert "cycle_crashed" in out and "bucket gone" in out
    assert '"CycleCompleted": 0' in out


def test_the_metric_line_is_valid_embedded_metric_format():
    line = metric_line(
        {"FrozenThisRun": (3, "Count"), "MissedFreezes": (0, "Count")},
        league="bl1",
        now=utc(2026, 10, 10, 12),
    )
    body = json.loads(line)
    directive = body["_aws"]["CloudWatchMetrics"][0]
    assert body["_aws"]["Timestamp"] == int(utc(2026, 10, 10, 12).timestamp() * 1000)
    assert directive["Namespace"] == "Spieltag"
    assert directive["Dimensions"] == [["League"]]
    assert {m["Name"] for m in directive["Metrics"]} == {"FrozenThisRun", "MissedFreezes"}
    assert body["League"] == "bl1" and body["FrozenThisRun"] == 3


def test_log_events_are_one_json_object_per_line(capsys):
    log_event("freeze_failed", error="x", match_ids=[1, 2], when=timedelta(hours=1))
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == "freeze_failed" and line["match_ids"] == [1, 2]


def test_the_run_command_prints_a_summary_and_exits_0(lake, tmp_path, monkeypatch, capsys):
    store, fetcher = lake
    monkeypatch.setattr(run_cli, "LocalStore", lambda root: store)
    monkeypatch.setattr(run_cli, "run_cycle", partial(run_cycle, fetcher=fetcher))

    code = run_cli.main(["--league", "sim", "--now", "2020-08-14T00:00:00+00:00", "--out", "x"])

    out = capsys.readouterr().out
    assert code == 0
    assert "froze 1 forecasts (1 in total)" in out


def test_the_run_command_exits_1_when_freezing_fails(lake, monkeypatch, capsys):
    store, fetcher = lake

    class Broken(ClimatologyModel):
        def fit(self, frame):
            raise RuntimeError("boom")

    monkeypatch.setattr(run_cli, "LocalStore", lambda root: store)
    monkeypatch.setattr(
        run_cli, "run_cycle", partial(run_cycle, fetcher=fetcher, factories={"b": Broken})
    )
    assert run_cli.main(["--league", "sim", "--now", "2020-08-14T00:00:00+00:00"]) == 1
    assert "freeze error" in capsys.readouterr().out


def test_the_run_command_exits_2_on_an_empty_store(tmp_path, capsys, monkeypatch):
    def nothing(league, season):
        raise ConnectionError("offline")

    monkeypatch.setattr(run_cli, "run_cycle", partial(run_cycle, fetcher=nothing))
    assert run_cli.main(["--league", "sim", "--out", str(tmp_path)]) == 2
    assert "Cannot run" in capsys.readouterr().out
    assert isinstance(LocalStore(tmp_path), LocalStore)


def test_a_full_cycle_works_on_s3_with_content_types_and_cache_headers(league_matches):
    client = FakeS3Client()
    store = S3Store("lake", client=client)
    _, fetcher = lake_with_history(league_matches, store=store)
    fetcher.now = NOW

    result = run_cycle(store, league="sim", now=NOW, fetcher=fetcher)

    assert result.frozen_now == 1
    key = ("lake", "site/data/live.json")
    assert client.extras[key] == {
        "ContentType": "application/json",
        "CacheControl": "public, max-age=60",
    }
    assert any(k.startswith("predictions/league=sim/season=2020/") for _, k in client.objects)
    again = run_cycle(store, league="sim", now=NOW, fetcher=fetcher)
    assert again.frozen_now == 0 and again.frozen_total == 1
