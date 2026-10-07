"""Structured logs and CloudWatch metrics, written as plain lines on standard output.

On Lambda, anything printed goes to CloudWatch Logs. A line in the Embedded Metric Format (EMF)
also becomes a CloudWatch metric, so the alarms need no extra API calls and no extra permissions.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

NAMESPACE = "Spieltag"


def log_event(event: str, *, stream: TextIO | None = None, **fields: Any) -> None:
    """One JSON object per line, easy to filter in CloudWatch Logs Insights."""
    line = {"event": event, **fields}
    print(json.dumps(line, default=str, sort_keys=True), file=stream or sys.stdout, flush=True)


def metric_line(
    metrics: dict[str, tuple[float, str]],
    *,
    league: str,
    now: datetime | None = None,
    namespace: str = NAMESPACE,
) -> str:
    """An EMF record. `metrics` maps a name to (value, unit), unit being "Count" or "None"."""
    stamp = int((now or datetime.now(UTC)).timestamp() * 1000)
    body: dict[str, Any] = {
        "_aws": {
            "Timestamp": stamp,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["League"]],
                    "Metrics": [
                        {"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()
                    ],
                }
            ],
        },
        "League": league,
    }
    body.update({name: value for name, (value, _) in metrics.items()})
    return json.dumps(body, sort_keys=True)


def emit_metrics(
    metrics: dict[str, tuple[float, str]],
    *,
    league: str,
    now: datetime | None = None,
    stream: TextIO | None = None,
) -> None:
    print(metric_line(metrics, league=league, now=now), file=stream or sys.stdout, flush=True)
