from datetime import UTC, datetime

import pytest

from spieltag.contracts import ContractError
from spieltag.pipeline import load_matches, read_curated, run_ingest, snapshot_id
from spieltag.store import LocalStore, S3Store

NOW = datetime(2025, 8, 31, 6, 0, 0, tzinfo=UTC)


def test_a_good_run_saves_raw_then_curated(raw_sample, tmp_path):
    store = LocalStore(tmp_path)
    result = run_ingest("bl1", 2025, store, now=NOW, fetcher=lambda league, season: raw_sample)

    assert result.matches == 4
    assert result.finished == 2
    assert (tmp_path / "raw/openligadb/bl1/2025/20250831T060000Z.json").exists()
    assert (tmp_path / "curated/matches/league=bl1/season=2025/matches.parquet").exists()

    curated = read_curated(store, "bl1", 2025)
    assert len(curated) == 4
    assert str(curated["kickoff_utc"].dt.tz) == "UTC"


def test_bad_data_keeps_the_raw_answer_but_writes_nothing_curated(raw_sample, tmp_path):
    raw_sample[0]["team2"]["teamId"] = raw_sample[0]["team1"]["teamId"]  # a team plays itself
    store = LocalStore(tmp_path)

    with pytest.raises(ContractError):
        run_ingest("bl1", 2025, store, now=NOW, fetcher=lambda league, season: raw_sample)

    assert (tmp_path / "raw/openligadb/bl1/2025/20250831T060000Z.json").exists()
    assert not (tmp_path / "curated").exists()


def test_snapshot_id_depends_only_on_the_data(raw_sample):
    shuffled_keys = [dict(reversed(list(match.items()))) for match in raw_sample]
    assert snapshot_id(raw_sample) == snapshot_id(shuffled_keys)
    assert len(snapshot_id(raw_sample)) == 12

    raw_sample[0]["matchIsFinished"] = False
    assert snapshot_id(raw_sample) != snapshot_id(shuffled_keys)


def test_local_store_refuses_keys_outside_its_folder(tmp_path):
    store = LocalStore(tmp_path / "data")
    with pytest.raises(ValueError, match="outside"):
        store.put_bytes("../escape.txt", b"x")


def test_local_store_round_trip(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes("a/b.txt", b"hello")
    assert store.get_bytes("a/b.txt") == b"hello"


class NoSuchKey(Exception):  # looks like the error boto3 raises
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body):  # noqa: N803 - boto3 uses these names
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey(Key)
        body = self.objects[(Bucket, Key)]
        return {"Body": type("Body", (), {"read": lambda self: body})()}


def test_s3_store_writes_to_the_given_bucket_and_returns_a_uri():
    client = FakeS3Client()
    store = S3Store("my-lake", client=client)
    uri = store.put_bytes("raw/x.json", b"{}")
    assert uri == "s3://my-lake/raw/x.json"
    assert client.objects[("my-lake", "raw/x.json")] == b"{}"
    assert store.get_bytes("raw/x.json") == b"{}"


def test_a_missing_object_is_a_file_not_found_error_for_both_stores(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalStore(tmp_path).get_bytes("nothing/here.txt")
    with pytest.raises(FileNotFoundError):
        S3Store("my-lake", client=FakeS3Client()).get_bytes("nothing/here.txt")


def test_load_matches_returns_what_exists_and_names_what_is_missing(raw_sample, tmp_path):
    store = LocalStore(tmp_path)
    run_ingest("bl1", 2025, store, now=NOW, fetcher=lambda league, season: raw_sample)

    matches, missing = load_matches(store, "bl1", [2024, 2025, 2026])

    assert len(matches) == 4
    assert missing == [2024, 2026]


def test_load_matches_with_nothing_stored_returns_an_empty_table(tmp_path):
    matches, missing = load_matches(LocalStore(tmp_path), "bl1", [2025])
    assert matches.empty and missing == [2025]


def test_the_same_match_id_in_two_seasons_is_rejected(raw_sample, tmp_path):
    store = LocalStore(tmp_path)
    run_ingest("bl1", 2025, store, now=NOW, fetcher=lambda league, season: raw_sample)
    run_ingest("bl1", 2024, store, now=NOW, fetcher=lambda league, season: raw_sample)
    with pytest.raises(ContractError, match="match_id"):
        load_matches(store, "bl1", [2024, 2025])
