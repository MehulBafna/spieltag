"""The AWS Lambda entry point. EventBridge Scheduler invokes it every hour."""

from __future__ import annotations

import os
from typing import Any

from spieltag.cycle import run_cycle
from spieltag.observability import emit_metrics, log_event
from spieltag.store import S3Store


class CycleFailed(RuntimeError):
    """The cycle ran but could not freeze forecasts. Raised so Lambda records an error."""


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    league = os.environ.get("SPIELTAG_LEAGUE", "bl1")
    store = S3Store(os.environ["SPIELTAG_BUCKET"])
    try:
        result = run_cycle(
            store, league=league, code_version=os.environ.get("SPIELTAG_CODE_VERSION", "unknown")
        )
    except Exception as exc:
        log_event("cycle_crashed", error=f"{type(exc).__name__}: {exc}")
        emit_metrics({"CycleCompleted": (0, "Count")}, league=league)
        raise
    emit_metrics(result.metrics(), league=league)
    if result.freeze_error:
        raise CycleFailed(result.freeze_error)
    return result.summary()
