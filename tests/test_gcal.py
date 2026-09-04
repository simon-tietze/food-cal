import datetime as dt

import pytest
from googleapiclient.errors import HttpError

from wmsync.gcal import GENERATOR_TAG, Plan, build_event, diff, is_retryable
from wmsync.models import MenuRecord

RECORD = MenuRecord(
    date=dt.date(2026, 9, 1),
    line="Menü 1",
    title="paniertes Schnitzel",
    price="5,00 EUR",
    allergens=("Ei", "Huhn"),
)


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "fake"


def http_error(status, reason=None):
    body = b'{"error": {"errors": [{"reason": "%s"}]}}' % (reason or "x").encode()
    return HttpError(FakeResp(status), body)


def test_event_shape():
    event = build_event(RECORD)
    assert event["summary"] == "paniertes Schnitzel"
    assert event["start"] == {"dateTime": "2026-09-01T13:00:00", "timeZone": "Europe/Berlin"}
    assert event["end"] == {"dateTime": "2026-09-01T13:30:00", "timeZone": "Europe/Berlin"}
    assert "Menü 1" in event["description"]
    assert "Ei, Huhn" in event["description"]
    assert event["extendedProperties"]["private"]["generator"] == GENERATOR_TAG
    assert event["extendedProperties"]["private"]["slot"] == "2026-09-01|Menü 1"


def test_diff_creates_when_absent():
    plan = diff([RECORD], {})
    assert len(plan.create) == 1 and not plan.update and not plan.delete


def test_diff_reports_unchanged_for_an_identical_event():
    existing = {build_event(RECORD)["id"]: build_event(RECORD)}
    plan = diff([RECORD], existing)
    assert len(plan.unchanged) == 1 and not plan.update


def test_diff_tolerates_offset_datetimes_from_the_api():
    remote = build_event(RECORD)
    remote["start"] = {"dateTime": "2026-09-01T13:00:00+02:00", "timeZone": "Europe/Berlin"}
    remote["end"] = {"dateTime": "2026-09-01T13:30:00+02:00", "timeZone": "Europe/Berlin"}
    plan = diff([RECORD], {remote["id"]: remote})
    assert len(plan.unchanged) == 1


def test_changed_order_updates_the_same_event():
    remote = build_event(RECORD)
    remote["summary"] = "Etwas ganz anderes"
    changed = MenuRecord(date=RECORD.date, line=RECORD.line, title="Neues Gericht")
    plan = diff([changed], {remote["id"]: remote})
    assert not plan.create
    assert len(plan.update) == 1
    assert plan.update[0][0]["id"] == remote["id"]


def test_vanished_slot_is_deleted():
    stale = build_event(RECORD)
    plan = diff([], {stale["id"]: stale})
    assert plan.delete == [stale]


def test_plan_summary():
    assert Plan(create=[1], delete=[2]).summary() == (
        "created=1 updated=0 unchanged=0 deleted=1"
    )


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (500, None, True),
        (503, None, True),
        (429, None, True),
        (403, "rateLimitExceeded", True),
        (403, "userRateLimitExceeded", True),
        (403, "forbidden", False),
        (401, None, False),
        (404, None, False),
        (409, None, False),
    ],
)
def test_retry_classification(status, reason, expected):
    assert is_retryable(http_error(status, reason)) is expected
