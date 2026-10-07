import io
import json

from spieltag import evaluate_cli
from spieltag.store import LocalStore


def _store_curated(store, matches):
    for season, frame in matches.groupby("season"):
        buffer = io.BytesIO()
        frame.to_parquet(buffer, index=False)
        store.put_bytes(
            f"curated/matches/league=sim/season={season}/matches.parquet", buffer.getvalue()
        )


ARGS = ["--league", "sim", "--first-season", "2016", "--last-season", "2020"]


def test_a_good_run_prints_the_table_and_saves_three_files(league_matches, tmp_path, capsys):
    _store_curated(LocalStore(tmp_path), league_matches)

    code = evaluate_cli.main(
        [*ARGS, "--first-test-season", "2018", "--min-train-matches", "500", "--out", str(tmp_path)]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "elo_ordered_logit" in out and "climatology" in out
    base = tmp_path / "evaluation" / "sim"
    assert (base / "predictions.parquet").exists()
    assert (base / "report.md").exists()
    report = json.loads((base / "report.json").read_text())
    assert report["data"]["first_test_season"] == 2018


def test_the_first_test_season_defaults_to_two_seasons_after_the_first(
    league_matches, tmp_path, capsys
):
    _store_curated(LocalStore(tmp_path), league_matches)
    code = evaluate_cli.main([*ARGS, "--min-train-matches", "500", "--out", str(tmp_path)])
    assert code == 0
    report = json.loads((tmp_path / "evaluation/sim/report.json").read_text())
    assert report["data"]["first_test_season"] == 2018


def test_missing_seasons_are_reported_but_do_not_stop_the_run(league_matches, tmp_path, capsys):
    present = league_matches[league_matches["season"] != 2017]
    _store_curated(LocalStore(tmp_path), present)

    code = evaluate_cli.main(
        [*ARGS, "--first-test-season", "2019", "--min-train-matches", "500", "--out", str(tmp_path)]
    )

    assert code == 0
    assert "no curated data for seasons [2017]" in capsys.readouterr().out


def test_an_empty_store_exits_2(tmp_path, capsys):
    assert evaluate_cli.main([*ARGS, "--out", str(tmp_path)]) == 2
    assert "spieltag-ingest" in capsys.readouterr().out


def test_too_little_history_exits_2(league_matches, tmp_path, capsys):
    _store_curated(LocalStore(tmp_path), league_matches)
    code = evaluate_cli.main([*ARGS, "--min-train-matches", "100000", "--out", str(tmp_path)])
    assert code == 2
    assert "Not enough history" in capsys.readouterr().out
