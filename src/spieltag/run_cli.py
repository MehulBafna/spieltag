"""Command line entry point: `spieltag-run --out data` runs one production cycle locally."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from spieltag.contracts import ContractError
from spieltag.cycle import DEFAULT_FIRST_SEASON, CycleError, run_cycle
from spieltag.store import LocalStore, S3Store, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spieltag-run",
        description="Run one production cycle: ingest, freeze due forecasts, score, publish.",
    )
    parser.add_argument("--league", default="bl1", help="league shortcut (default: bl1)")
    parser.add_argument(
        "--first-season", type=int, default=DEFAULT_FIRST_SEASON, help="oldest season to load"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="also ingest every season from --first-season on (for an empty store)",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="pretend it is this UTC time (ISO 8601); for replays and demos, never for production",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out", default="data", help="local folder to use (default: data)")
    target.add_argument("--bucket", help="use this S3 bucket instead of a local folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store: Store = S3Store(args.bucket) if args.bucket else LocalStore(args.out)
    now = datetime.fromisoformat(args.now).astimezone(UTC) if args.now else None
    try:
        result = run_cycle(
            store,
            league=args.league,
            now=now,
            first_season=args.first_season,
            backfill=args.backfill,
        )
    except (CycleError, ContractError) as exc:
        print(f"Cannot run: {exc}")
        return 2

    print(
        f"{result.league} {result.season} at {result.now}: ingest {result.ingest_status}, "
        f"froze {result.frozen_now} forecasts ({result.frozen_total} in total), "
        f"{result.scored} scored, {len(result.missed)} missed"
    )
    for problem in result.problems:
        print(f"  problem: {problem}")
    if result.ingest_error:
        print(f"  ingest error: {result.ingest_error}")
    if result.freeze_error:
        print(f"  freeze error: {result.freeze_error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
