import datetime as dt

import pytest

from wmsync.eventid import ID_PREFIX, event_id, is_valid_event_id, slot_key

ALLOWED = set("abcdefghijklmnopqrstuv0123456789")


def test_slot_key_shape():
    assert slot_key(dt.date(2026, 9, 1), "Menü 1") == "2026-09-01|Menü 1"


def test_id_is_valid_base32hex():
    ident = event_id(slot_key(dt.date(2026, 9, 1), "Menü 1"))
    assert ident.startswith(ID_PREFIX)
    assert set(ident) <= ALLOWED
    assert 5 <= len(ident) <= 1024
    assert is_valid_event_id(ident)


def test_id_is_deterministic():
    key = slot_key(dt.date(2026, 9, 1), "Vegetarisch")
    assert event_id(key) == event_id(key)


def test_id_ignores_the_food_name():
    """Changing an order must reuse the slot's event, not create a new one."""
    key = slot_key(dt.date(2026, 9, 1), "Menü 1")
    assert event_id(key) == event_id("2026-09-01|Menü 1")


@pytest.mark.parametrize(
    "a,b",
    [
        (("2026-09-01", "Menü 1"), ("2026-09-01", "Vegetarisch")),
        (("2026-09-01", "Menü 1"), ("2026-09-02", "Menü 1")),
    ],
)
def test_distinct_slots_get_distinct_ids(a, b):
    assert event_id("|".join(a)) != event_id("|".join(b))


def test_no_collisions_over_a_year_of_slots():
    day = dt.date(2026, 1, 1)
    ids = {
        event_id(slot_key(day + dt.timedelta(days=n), line))
        for n in range(365)
        for line in ("Menü 1", "Vegetarisch", "Dessert")
    }
    assert len(ids) == 365 * 3


def test_prefix_stays_inside_the_alphabet():
    assert set(ID_PREFIX) <= ALLOWED
