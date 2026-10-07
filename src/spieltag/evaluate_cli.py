"""Command line entry point: `spieltag-evaluate --bucket my-lake`."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from spieltag.contracts import ContractError
from spieltag.evaluate import build_report, render_markdown, save_evaluation, walk_forward
from spieltag.features import DEFAULT_LEAD, build_features
from spieltag.ingest import current_season
from spieltag.models import default_models
from spieltag.pipeline import load_matches
from spieltag.store import LocalStore, S3Store, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spieltag-evaluate",
        description="Walk-forward evaluation of the forecasting models on ingested seasons.",
    )
    parser.add_argument("--league", default="bl1", help="league shortcut (default: bl1)")
    parser.add_argument(
        "--first-season", type=int, default=2016, help="first season to load (default: 2016)"
    )
    parser.add_argument(
        "--last-season", type=int, default=None, help="last season to load (default: current)"
    )
    parser.add_argument(
        "--first-test-season",
        type=int,
        default=None,
        help="first season to predict (default: two seasons after the first loaded one)",
    )
    parser.add_argument(
        "--lead-hours",
        type=float,
        default=DEFAULT_LEAD.total_seconds() / 3600,
        help="predictions are made this many hours before the first kickoff (default: 24)",
    )
    parser.add_argument(
        "--min-train-matches",
        type=int,
        default=600,
        help="skip matchdays with fewer finished training matches (default: 600)",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--out", default="data", help="local folder to read and write (default: data)"
    )
    target.add_argument("--bucket", help="read and write this S3 bucket instead of a local folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store: Store = S3Store(args.bucket) if args.bucket else LocalStore(args.out)
    last = args.last_season if args.last_season is not None else current_season()
    seasons = list(range(args.first_season, last + 1))

    try:
        matches, missing = load_matches(store, args.league, seasons)
    except ContractError as exc:
        print(f"The stored data is inconsistent: {exc}")
        return 2
    if missing:
        print(f"Warning: no curated data for seasons {missing}. Run spieltag-ingest for them.")
    if matches.empty:
        print("No curated matches found. Run spieltag-ingest first.")
        return 2

    loaded = sorted(int(s) for s in matches["season"].unique())
    first_test = args.first_test_season if args.first_test_season is not None else loaded[0] + 2
    lead = timedelta(hours=args.lead_hours)

    features = build_features(matches, lead=lead)
    predictions = walk_forward(
        features,
        default_models(),
        first_test_season=first_test,
        lead=lead,
        min_train_matches=args.min_train_matches,
    )
    if predictions.empty:
        print(
            f"Not enough history: need at least {args.min_train_matches} finished matches before "
            f"season {first_test}. Ingest more seasons or lower --first-test-season."
        )
        return 2

    report = build_report(
        predictions,
        league=args.league,
        seasons_loaded=loaded,
        first_test_season=first_test,
        lead=lead,
        min_train_matches=args.min_train_matches,
    )
    locations = save_evaluation(store, args.league, predictions, report)
    print(render_markdown(report))
    print(f"Saved:\n  {locations['report_md']}\n  {locations['report_json']}")
    print(f"  {locations['predictions']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
