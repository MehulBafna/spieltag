from datetime import UTC, datetime

import pytest

from spieltag.contracts import ContractError
from spieltag.pipeline import read_curated, run_ingest, snapshot_id
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


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body):  # noqa: N803 - boto3 uses these names
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):  # noqa: N803
        body = self.objects[(Bucket, Key)]
        return {"Body": type("Body", (), {"read": lambda self: body})()}


def test_s3_store_writes_to_the_given_bucket_and_returns_a_uri():
    client = FakeS3Client()
    store = S3Store("my-lake", client=client)
    uri = store.put_bytes("raw/x.json", b"{}")
    assert uri == "s3://my-lake/raw/x.json"
    assert client.objects[("my-lake", "raw/x.json")] == b"{}"
    assert store.get_bytes("raw/x.json") == b"{}"
