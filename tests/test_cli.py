import pytest

from spieltag import cli, pipeline
from spieltag.ingest import OpenLigaError


def _use_fetcher(monkeypatch, fetcher):
    real_run_ingest = pipeline.run_ingest
    monkeypatch.setattr(
        cli,
        "run_ingest",
        lambda league, season, store: real_run_ingest(league, season, store, fetcher=fetcher),
    )


def test_a_good_run_exits_0_and_reports_the_counts(monkeypatch, raw_sample, tmp_path, capsys):
    _use_fetcher(monkeypatch, lambda league, season: raw_sample)

    code = cli.main(["--season", "2025", "--out", str(tmp_path)])

    assert code == 0
    assert "4 matches, 2 finished" in capsys.readouterr().out
    assert list(tmp_path.glob("curated/matches/league=bl1/season=2025/matches.parquet"))


def test_bad_data_exits_2_and_says_the_raw_answer_was_kept(
    monkeypatch, raw_sample, tmp_path, capsys
):
    raw_sample[0]["matchResults"] = []  # a finished match without a result
    _use_fetcher(monkeypatch, lambda league, season: raw_sample)

    code = cli.main(["--season", "2025", "--out", str(tmp_path)])

    assert code == 2
    assert "raw answer was saved" in capsys.readouterr().out
    assert list(tmp_path.glob("raw/openligadb/bl1/2025/*.json"))
    assert not (tmp_path / "curated").exists()


def test_an_api_problem_exits_3(monkeypatch, tmp_path, capsys):
    def broken(league, season):
        raise OpenLigaError("giving up")

    _use_fetcher(monkeypatch, broken)

    assert cli.main(["--season", "2025", "--out", str(tmp_path)]) == 3
    assert "OpenLigaDB problem" in capsys.readouterr().out


def test_out_and_bucket_cannot_be_combined():
    with pytest.raises(SystemExit):
        cli.main(["--out", "x", "--bucket", "y"])
