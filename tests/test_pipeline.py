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


class FakeS3Error(Exception):  # looks like the errors boto3 raises
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    page_size = 1000

    def __init__(self):
        self.objects = {}
        self.extras = {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, **extra):  # noqa: N803
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise FakeS3Error("PreconditionFailed")
        self.objects[(Bucket, Key)] = Body
        self.extras[(Bucket, Key)] = extra

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("NoSuchKey")
        body = self.objects[(Bucket, Key)]
        return {"Body": type("Body", (), {"read": lambda self: body})()}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        result = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        if more:
            result["NextContinuationToken"] = str(start + self.page_size)
        return result


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


def test_only_if_changed_writes_nothing_the_second_time(raw_sample, tmp_path):
    store = LocalStore(tmp_path)

    def fetch(league, season):
        return raw_sample

    first = run_ingest("bl1", 2025, store, now=NOW, fetcher=fetch, only_if_changed=True)
    files_after_first = store.list_keys("")
    second = run_ingest("bl1", 2025, store, now=NOW, fetcher=fetch, only_if_changed=True)

    assert first.changed and not second.changed
    assert second.matches == 4 and second.finished == 2
    assert store.list_keys("") == files_after_first  # not even a new raw snapshot


def test_only_if_changed_ingests_again_when_the_data_changed(raw_sample, tmp_path):
    store = LocalStore(tmp_path)
    run_ingest(
        "bl1", 2025, store, now=NOW, fetcher=lambda league, season: raw_sample, only_if_changed=True
    )
    raw_sample[2]["matchIsFinished"] = True
    raw_sample[2]["matchResults"] = [
        {
            "resultID": 1,
            "resultName": "Endergebnis",
            "pointsTeam1": 1,
            "pointsTeam2": 0,
            "resultOrderID": 1,
            "resultTypeID": 2,
        }
    ]
    again = run_ingest(
        "bl1", 2025, store, now=NOW, fetcher=lambda league, season: raw_sample, only_if_changed=True
    )
    assert again.changed and again.finished == 3


def test_an_empty_answer_is_not_an_error_for_a_scheduled_run(tmp_path):
    result = run_ingest(
        "bl1", 2027, LocalStore(tmp_path), fetcher=lambda league, season: [], only_if_changed=True
    )
    assert not result.changed and result.matches == 0
    assert LocalStore(tmp_path).list_keys("") == []


def test_a_failed_validation_leaves_no_marker_so_the_next_run_retries(raw_sample, tmp_path):
    store = LocalStore(tmp_path)
    raw_sample[0]["team2"]["teamId"] = raw_sample[0]["team1"]["teamId"]
    with pytest.raises(ContractError):
        run_ingest(
            "bl1",
            2025,
            store,
            now=NOW,
            fetcher=lambda league, season: raw_sample,
            only_if_changed=True,
        )
    assert not [k for k in store.list_keys("curated")]


def test_put_if_absent_never_overwrites_in_either_store(tmp_path):
    for store in (LocalStore(tmp_path), S3Store("my-lake", client=FakeS3Client())):
        assert store.put_bytes_if_absent("p/a.json", b"first") is True
        assert store.put_bytes_if_absent("p/a.json", b"second") is False
        assert store.get_bytes("p/a.json") == b"first"


def test_list_keys_filters_by_prefix_and_pages_through_s3(tmp_path):
    local = LocalStore(tmp_path)
    for key in ("a/1.txt", "a/2.txt", "b/1.txt"):
        local.put_bytes(key, b"x")
    assert local.list_keys("a/") == ["a/1.txt", "a/2.txt"]

    client = FakeS3Client()
    client.page_size = 2
    s3 = S3Store("my-lake", client=client)
    for i in range(5):
        s3.put_bytes(f"p/{i}.txt", b"x")
    s3.put_bytes("q/0.txt", b"x")
    assert s3.list_keys("p/") == [f"p/{i}.txt" for i in range(5)]
