"""Daily tenant-wide alert pipeline scheduler.

Run once from cron with ``python -m backend.scheduler --once`` or keep the
process alive with ``python -m backend.scheduler``. C5 reasoning follows the
``ENABLE_REASONING`` environment setting unless explicitly overridden.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _request_json(api_url: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{api_url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def run_once(
    grain: str = "month",
    enable_reasoning: bool | None = None,
    api_url: str = "http://127.0.0.1:8000",
) -> dict:
    """Run the tenant-wide alert pipeline through the running API."""
    periods = _request_json(api_url, f"/alerts/periods?grain={grain}")["periods"]
    if not periods:
        raise RuntimeError(f"No completed {grain} periods are available")
    period = periods[-1]
    payload = {"period": period, "grain": grain}
    if enable_reasoning is not None:
        payload["enable_reasoning"] = enable_reasoning
    logger.info("Starting alert pipeline for period %s via %s", period, api_url)
    result = _request_json(api_url, "/alerts/run", method="POST", payload=payload)
    logger.info(
        "Alert run %s completed: %s alerts across %s tenants for %s",
        result["run_id"],
        result["alerts_saved"],
        len(result["tenants"]),
        period,
    )
    return result


def _seconds_until_next_run(hour: int, minute: int) -> float:
    now = datetime.now()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return max((next_run - now).total_seconds(), 1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily MoveInsight alert pipeline")
    parser.add_argument("--once", action="store_true", help="run one pipeline execution and exit")
    parser.add_argument("--grain", choices=["month", "week", "day"], default="month")
    parser.add_argument("--hour", type=int, default=2, help="daily run hour, local time")
    parser.add_argument("--minute", type=int, default=0, help="daily run minute, local time")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("MOVEINSIGHT_API_URL", "http://127.0.0.1:8000"),
        help="running MoveInsight API base URL",
    )
    reasoning = parser.add_mutually_exclusive_group()
    reasoning.add_argument("--enable-reasoning", dest="enable_reasoning", action="store_true")
    reasoning.add_argument("--disable-reasoning", dest="enable_reasoning", action="store_false")
    parser.set_defaults(enable_reasoning=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        run_once(args.grain, args.enable_reasoning, args.api_url)
        return

    while True:
        delay = _seconds_until_next_run(args.hour, args.minute)
        logger.info("Next alert pipeline run in %.0f seconds", delay)
        time.sleep(delay)
        try:
            run_once(args.grain, args.enable_reasoning, args.api_url)
        except Exception:
            logger.exception("Scheduled alert pipeline failed")


if __name__ == "__main__":
    main()