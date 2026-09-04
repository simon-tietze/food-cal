"""Command line entry point."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("wmsync")

DEFAULT_CREDS = "creds.json"
DEFAULT_SA_KEY = "majestic-poetry-94110-ff3cbc4b22a6.json"
DEFAULT_CALENDAR_ID = (
    "4d713e2402ffce8ce8e67e3a83bf95fda665ba2b12b2de55bec58d6021ca3c68"
    "@group.calendar.google.com"
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="webmenue-sync",
        description="Mirror the selected WebMenue lunch order into a Google Calendar.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full scrape and diff but make no calendar writes",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=4,
        metavar="N",
        help="maximum number of weeks to page forward (default: 4)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument(
        "--calendar-id",
        default=os.environ.get("GOOGLE_CALENDAR_ID", DEFAULT_CALENDAR_ID),
        help="target calendar ID (never 'primary'; a service account has none)",
    )
    parser.add_argument(
        "--creds",
        type=Path,
        default=Path(os.environ.get("WEBMENUE_CREDS", DEFAULT_CREDS)),
        help="path to the WebMenue credentials JSON (env: WEBMENUE_CREDS)",
    )
    parser.add_argument(
        "--sa-key",
        type=Path,
        default=Path(os.environ.get("GOOGLE_SA_KEY", DEFAULT_SA_KEY)),
        help="path to the Google service account key JSON (env: GOOGLE_SA_KEY)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("WEBMENUE_URL", "https://wiaf.webmenue.info/"),
        help="WebMenue base URL",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "proceed even when the scrape found no selected items; without this "
            "an empty scrape is treated as a failure and nothing is deleted"
        ),
    )
    args = parser.parse_args(argv)
    if args.weeks < 1:
        parser.error("--weeks must be at least 1")
    return args


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def _log_plan(plan, records) -> None:
    for record in records:
        log.debug("scraped %s %s: %s", record.date, record.line, record.title)
    for event in plan.create:
        log.info("CREATE  %s  %s", event["start"]["dateTime"][:10], event["summary"])
    for desired, existing in plan.update:
        log.info(
            "UPDATE  %s  %r -> %r",
            desired["start"]["dateTime"][:10],
            existing.get("summary"),
            desired["summary"],
        )
    for event in plan.unchanged:
        log.info("UNCHANGED %s  %s", event["start"]["dateTime"][:10], event["summary"])
    for event in plan.delete:
        start = (event.get("start") or {}).get("dateTime", "?")[:10]
        log.info("DELETE  %s  %s", start, event.get("summary"))


def run(args) -> int:
    # Imported here so --help works without the Google client libraries present.
    from .gcal import CalendarSync, diff
    from .scraper import ScrapeError, WebMenueClient, extract_credentials

    for path, label in ((args.creds, "credentials"), (args.sa_key, "service account key")):
        if not path.is_file():
            log.error("%s file not found: %s", label, path)
            return 2

    user, password = extract_credentials(json.loads(args.creds.read_text(encoding="utf-8")))

    client = WebMenueClient(args.base_url)
    client.login(user, password)
    records, covered_days = client.scrape(max_weeks=args.weeks)

    log.info(
        "scraped %d selected item(s) across %d day(s)", len(records), len(covered_days)
    )

    if not covered_days:
        raise ScrapeError("no menu days found at all - login or DOM likely broken")
    if not records and not args.allow_empty:
        raise ScrapeError(
            "scrape returned zero selected items; refusing to delete anything. "
            "Pass --allow-empty if this is genuinely correct."
        )

    calendar = CalendarSync(args.sa_key, args.calendar_id, dry_run=args.dry_run)
    existing = calendar.fetch_window(covered_days[0], covered_days[-1])
    plan = diff(records, existing)
    _log_plan(plan, records)

    if args.dry_run:
        log.info("DRY RUN - no writes performed. Would be: %s", plan.summary())
        return 0

    calendar.apply(plan)
    log.info("sync complete: %s", plan.summary())
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - top level: report and exit non-zero
        log.error("%s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
