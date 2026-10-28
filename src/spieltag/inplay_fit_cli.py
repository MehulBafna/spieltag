"""Command line entry point: `spieltag-inplay-fit --bucket my-lake`.

Reads the raw match snapshots (they hold every goal with its minute) and the walk-forward
predictions written by `spieltag-evaluate`, estimates the in-play parameters, and checks them on
the latest seasons, which the fit never saw. With `--write` the fitted parameters are stored for
the live job, but only if they beat the neutral model on those held-out seasons.
"""

from __future__ import annotations

import argparse
import json
import sys

from spieltag.inplay import InplayParams, params_key
from spieltag.inplay_fit import (
    evaluate_variants,
    fit_params,
    load_goal_matches,
    paired_over_matches,
    predictions_from_store,
    render_report,
    summarize_variants,
)
from spieltag.store import LocalStore, S3Store, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spieltag-inplay-fit",
        description="Estimate in-play parameters from past goals; check them on later seasons.",
    )
    parser.add_argument("--league", default="bl1", help="league shortcut (default: bl1)")
    parser.add_argument(
        "--held-out-seasons",
        type=int,
        default=2,
        help="the latest seasons kept aside to test the fitted parameters (default: 2)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="store the parameters (fitted on all seasons) if they beat neutral on held-out data",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out", default="data", help="local folder to read and write")
    target.add_argument("--bucket", help="read and write this S3 bucket instead of a local folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store: Store = S3Store(args.bucket) if args.bucket else LocalStore(args.out)

    try:
        predictions = predictions_from_store(store, args.league)
    except FileNotFoundError:
        print("No walk-forward predictions found. Run spieltag-evaluate first.")
        return 2
    matches, skipped = load_goal_matches(store, args.league, predictions)
    if not matches:
        print("No finished matches with complete goal lists were found in the raw snapshots.")
        return 2

    seasons = sorted({m.season for m in matches})
    cut = seasons[-args.held_out_seasons] if len(seasons) > args.held_out_seasons else None
    train = [m for m in matches if cut is None or m.season < cut]
    test = [m for m in matches if cut is not None and m.season >= cut]

    train_params, train_detail = fit_params(
        train, f"fitted on seasons {train[0].season}-{train[-1].season}"
    )
    held_out = pairs = None
    better = False
    if test:
        variants = {"neutral": InplayParams(), "fitted": train_params}
        scores = evaluate_variants(test, variants)
        held_out = summarize_variants(scores)[["sealed", "neutral", "fitted"]]
        pairs = {
            "live (neutral) minus sealed": paired_over_matches(scores, "neutral", "sealed"),
            "live (fitted) minus sealed": paired_over_matches(scores, "fitted", "sealed"),
            "live (fitted) minus live (neutral)": paired_over_matches(scores, "fitted", "neutral"),
        }
        better = pairs["live (fitted) minus live (neutral)"]["high"] < 0

    print(
        render_report(
            train_detail["states"],
            train_params.first_half_share,
            train_detail["first_half_goals"],
            held_out,
            pairs,
            skipped,
            len(train),
        )
    )
    if not args.write:
        return 0
    if not test:
        print("Not written: there are not enough seasons to hold any out.")
        return 1
    if not better:
        print("Not written: the fitted parameters do not clearly beat neutral on held-out seasons.")
        return 1

    final_params, _ = fit_params(
        matches,
        f"fitted on seasons {seasons[0]}-{seasons[-1]}; held-out check on {cut}-{seasons[-1]}",
    )
    uri = store.put_bytes(
        params_key(args.league), json.dumps(final_params.to_dict(), indent=2).encode()
    )
    print(f"Written: {uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
