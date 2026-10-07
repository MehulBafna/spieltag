"""Command line entry point: `spieltag-ingest --season 2025`."""

from __future__ import annotations

import argparse
import sys

from spieltag.contracts import ContractError
from spieltag.ingest import OpenLigaError, current_season
from spieltag.pipeline import run_ingest
from spieltag.store import LocalStore, S3Store, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spieltag-ingest",
        description="Fetch one Bundesliga season from OpenLigaDB, validate it and store it.",
    )
    parser.add_argument("--league", default="bl1", help="OpenLigaDB league shortcut (default: bl1)")
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="season start year, e.g. 2025 for 2025/26 (default: the current season)",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out", default="data", help="local folder to write to (default: data)")
    target.add_argument("--bucket", help="write to this S3 bucket instead of a local folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    season = args.season if args.season is not None else current_season()
    store: Store = S3Store(args.bucket) if args.bucket else LocalStore(args.out)

    try:
        result = run_ingest(args.league, season, store)
    except ContractError as exc:
        print(f"Validation failed. The raw answer was saved, nothing curated changed.\n{exc}")
        return 2
    except OpenLigaError as exc:
        print(f"OpenLigaDB problem: {exc}")
        return 3

    print(
        f"{result.league} {result.season}: {result.matches} matches, {result.finished} finished\n"
        f"snapshot {result.snapshot_id}\nraw     {result.raw_uri}\ncurated {result.curated_uri}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
