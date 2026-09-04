"""Google Calendar side: build events, diff them, and write them idempotently."""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .eventid import event_id
from .models import MenuRecord

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TIMEZONE = "Europe/Berlin"
LUNCH_START = dt.time(13, 0)
LUNCH_END = dt.time(13, 30)
GENERATOR_TAG = "webmenue-gcal-sync"
GENERATOR_NOTE = "Automatisch erzeugt von webmenue-gcal-sync."

MAX_ATTEMPTS = 5
BASE_BACKOFF = 1.0
RETRYABLE_403_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "backendError",
}


class CalendarError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# retry helper
# --------------------------------------------------------------------------
def _reasons(error: HttpError) -> set[str]:
    """Reason strings from the error body.

    Read straight from the payload rather than ``error.error_details``, which
    the client only populates once it has managed to parse a full error object.
    """
    out: set[str] = set()
    try:
        payload = json.loads(error.content.decode("utf-8", "replace"))
    except (AttributeError, ValueError):
        return out
    error_obj = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error_obj, dict):
        return out
    if error_obj.get("status"):
        out.add(str(error_obj["status"]))
    for item in error_obj.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            out.add(str(item["reason"]))
    return out


def is_retryable(error: HttpError) -> bool:
    """5xx, 429, and rate-limit-flavoured 403s. Never plain auth/4xx errors."""
    status = getattr(error.resp, "status", None)
    if status is None:
        return False
    if status >= 500 or status == 429:
        return True
    if status == 403:
        return bool(_reasons(error) & RETRYABLE_403_REASONS)
    return False


def execute(request, *, sleep=time.sleep):
    """Run a Google API request with exponential backoff on transient errors."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except HttpError as err:
            if attempt == MAX_ATTEMPTS or not is_retryable(err):
                raise
            delay = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "Google API returned %s (attempt %d/%d); retrying in %.1fs",
                getattr(err.resp, "status", "?"),
                attempt,
                MAX_ATTEMPTS,
                delay,
            )
            sleep(delay)


# --------------------------------------------------------------------------
# event construction
# --------------------------------------------------------------------------
def build_description(record: MenuRecord) -> str:
    lines = [f"Menülinie: {record.line}"]
    if record.price:
        lines.append(f"Preis: {record.price}")
    if record.allergens:
        lines.append("Unverträglichkeiten: " + ", ".join(record.allergens))
    lines.append("")
    lines.append(GENERATOR_NOTE)
    return "\n".join(lines)


def build_event(record: MenuRecord) -> dict:
    start = dt.datetime.combine(record.date, LUNCH_START)
    end = dt.datetime.combine(record.date, LUNCH_END)
    return {
        "id": event_id(record.slot_key),
        "summary": record.title,
        "description": build_description(record),
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
        "extendedProperties": {
            "private": {"generator": GENERATOR_TAG, "slot": record.slot_key}
        },
    }


def _comparable(event: dict) -> tuple:
    """The fields we actually care about, so unchanged runs write nothing."""
    return (
        event.get("summary"),
        event.get("description"),
        (event.get("start") or {}).get("dateTime"),
        (event.get("start") or {}).get("timeZone"),
        (event.get("end") or {}).get("dateTime"),
        (event.get("end") or {}).get("timeZone"),
    )


def _normalise_remote(event: dict) -> dict:
    """Re-express a fetched event so it compares like a freshly built one."""

    def side(key):
        raw = (event.get(key) or {}).get("dateTime")
        tz = (event.get(key) or {}).get("timeZone") or TIMEZONE
        if not raw:
            return {"dateTime": None, "timeZone": tz}
        moment = dt.datetime.fromisoformat(raw)
        if moment.tzinfo is not None:
            moment = moment.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        return {"dateTime": moment.isoformat(), "timeZone": tz}

    return {
        "summary": event.get("summary"),
        "description": event.get("description"),
        "start": side("start"),
        "end": side("end"),
    }


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------
@dataclass
class Plan:
    create: list[dict] = field(default_factory=list)
    update: list[tuple[dict, dict]] = field(default_factory=list)  # (desired, existing)
    unchanged: list[dict] = field(default_factory=list)
    delete: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"created={len(self.create)} updated={len(self.update)} "
            f"unchanged={len(self.unchanged)} deleted={len(self.delete)}"
        )


def diff(records: list[MenuRecord], existing: dict[str, dict]) -> Plan:
    """Compare desired events against what is already on the calendar."""
    plan = Plan()
    desired_ids = set()
    for record in records:
        event = build_event(record)
        desired_ids.add(event["id"])
        current = existing.get(event["id"])
        if current is None:
            plan.create.append(event)
        elif _comparable(event) != _comparable(_normalise_remote(current)):
            plan.update.append((event, current))
        else:
            plan.unchanged.append(event)
    for event_key, event in existing.items():
        if event_key not in desired_ids:
            plan.delete.append(event)
    return plan


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
class CalendarSync:
    def __init__(self, key_path, calendar_id: str, dry_run: bool = False):
        credentials = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=SCOPES
        )
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self.calendar_id = calendar_id
        self.dry_run = dry_run

    def fetch_window(self, first_day: dt.date, last_day: dt.date) -> dict[str, dict]:
        """Every event this tool owns inside the covered date range.

        Both bounds come from the weeks the scrape actually visited, so nothing
        outside that window can ever be considered for deletion.
        """
        tz = ZoneInfo(TIMEZONE)
        time_min = dt.datetime.combine(first_day, dt.time.min, tzinfo=tz)
        time_max = dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

        found: dict[str, dict] = {}
        page_token = None
        while True:
            response = execute(
                self.service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    showDeleted=False,
                    maxResults=250,
                    pageToken=page_token,
                    privateExtendedProperty=f"generator={GENERATOR_TAG}",
                )
            )
            for event in response.get("items", []):
                if event.get("id"):
                    found[event["id"]] = event
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        log.info(
            "found %d existing generated events between %s and %s",
            len(found),
            first_day,
            last_day,
        )
        return found

    # -- mutations --------------------------------------------------------
    def apply(self, plan: Plan) -> None:
        for event in plan.create:
            self._insert(event)
        for desired, _existing in plan.update:
            self._update(desired)
        for event in plan.delete:
            self._delete(event["id"])

    def _insert(self, event: dict) -> None:
        if self.dry_run:
            return
        try:
            execute(self.service.events().insert(calendarId=self.calendar_id, body=event))
        except HttpError as err:
            if getattr(err.resp, "status", None) != 409:
                raise
            log.debug("event %s already exists, updating instead", event["id"])
            self._update(event)

    def _update(self, event: dict) -> None:
        if self.dry_run:
            return
        body = {k: v for k, v in event.items() if k != "id"}
        execute(
            self.service.events().update(
                calendarId=self.calendar_id, eventId=event["id"], body=body
            )
        )

    def _delete(self, identifier: str) -> None:
        if self.dry_run:
            return
        try:
            execute(self.service.events().delete(calendarId=self.calendar_id, eventId=identifier))
        except HttpError as err:
            if getattr(err.resp, "status", None) == 410:
                log.debug("event %s already gone", identifier)
                return
            raise
