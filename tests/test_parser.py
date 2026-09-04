import datetime as dt
from pathlib import Path

import pytest

from wmsync.parser import parse_hidden_fields, parse_postback_target, parse_week

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ordered_week():
    return parse_week(load("week_ordered.html"))


@pytest.fixture(scope="module")
def empty_week():
    return parse_week(load("week_freeday_none_ordered.html"))


def test_week_days_are_the_five_workdays(ordered_week):
    assert ordered_week.week_start == dt.date(2026, 8, 31)
    assert ordered_week.days == tuple(
        dt.date(2026, 8, 31) + dt.timedelta(days=n) for n in range(5)
    )


def test_only_selected_items_are_returned(ordered_week):
    assert ordered_week.offered_count == 10
    assert len(ordered_week.records) == 5
    assert [r.date for r in ordered_week.records] == list(ordered_week.days)


def test_record_contract(ordered_week):
    record = next(r for r in ordered_week.records if r.date == dt.date(2026, 9, 1))
    assert record.as_dict() == {
        "date": dt.date(2026, 9, 1),
        "line": "Menü 1",
        "title": record.title,
    }
    assert record.title.startswith("paniertes Schnitzel")
    assert record.slot_key == "2026-09-01|Menü 1"


def test_line_reflects_the_menu_slot_not_the_dish(ordered_week):
    lines = {r.date: r.line for r in ordered_week.records}
    assert lines[dt.date(2026, 8, 31)] == "Vegetarisch"
    assert lines[dt.date(2026, 9, 1)] == "Menü 1"


def test_titles_are_whitespace_normalised(ordered_week):
    for record in ordered_week.records:
        assert "\n" not in record.title
        assert "  " not in record.title


def test_extras_are_captured(ordered_week):
    record = next(r for r in ordered_week.records if r.date == dt.date(2026, 9, 2))
    assert record.price == "5,00 EUR"
    assert record.allergens == ("Fisch", "Getreide")


def test_week_with_nothing_ordered_still_reports_its_days(empty_week):
    assert empty_week.records == ()
    assert empty_week.has_menu is True
    assert empty_week.days[0] == dt.date(2026, 9, 28)
    assert empty_week.days[-1] == dt.date(2026, 10, 2)


def test_freeday_placeholder_cards_are_ignored(empty_week):
    # The page carries 12 cards, two of which are empty "freeday" placeholders.
    assert empty_week.offered_count == 10


def test_hidden_fields_include_viewstate():
    fields = parse_hidden_fields(load("login.html"))
    assert "__VIEWSTATE" in fields
    assert "__EVENTVALIDATION" in fields
    assert fields["__VIEWSTATEGENERATOR"]


def test_postback_target_extraction():
    assert parse_postback_target(
        "javascript:__doPostBack('ctl00$MainContent$ctl03','')"
    ) == ("ctl00$MainContent$ctl03", "")
    assert parse_postback_target("https://example.invalid/") is None
    assert parse_postback_target(None) is None


def test_next_week_link_is_present_and_parseable():
    from wmsync.parser import make_soup

    link = make_soup(load("week_ordered.html")).select_one(".week-next")
    assert parse_postback_target(link["href"])[0].startswith("ctl00$MainContent$")
