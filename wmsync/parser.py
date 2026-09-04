"""Pure parsing of WebMenue BillofFare.aspx HTML.

Deliberately free of any network access so it can be unit tested against the
saved fixtures in ``tests/fixtures``.

Every menu card on the page carries a ``data-ntc-billoffare-element``
attribute: base64-encoded JSON with the authoritative date, menu line, price,
title and -- crucially -- ``QuantityOrdered``. That is how a *selected* item is
told apart from a merely offered one; the ``ordered``/``notordered`` CSS class
on the card agrees with it, and we require both to be consistent.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import re

from bs4 import BeautifulSoup

from .models import MenuRecord, WeekPage

log = logging.getLogger(__name__)

# The pages are served as ISO-8859-15, declared via <meta charset>.
PAGE_ENCODING = "iso-8859-15"

CARD_ATTR = "data-ntc-billoffare-element"
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def decode_page(raw: bytes) -> str:
    """Decode a raw response body using the site's declared encoding."""
    return raw.decode(PAGE_ENCODING, errors="replace")


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _parse_german_date(text: str) -> dt.date | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    return dt.date(year, month, day)


def _card_payload(card) -> dict | None:
    raw = card.get(CARD_ATTR) or ""
    if not raw.strip():
        # "freeday"/"empty" placeholder cards carry an empty attribute.
        return None
    try:
        return json.loads(base64.b64decode(raw))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        log.warning("undecodable menu card payload, skipping: %s", exc)
        return None


def _allergens_for(soup: BeautifulSoup, card) -> tuple[str, ...]:
    """Allergen ("Unvertraeglichkeiten") names from the card's detail modal."""
    card_id = card.get("id")
    if not card_id:
        return ()
    modal = soup.select_one(f'div[id="modal-billoffareitemdetails-{card_id}"]')
    if modal is None:
        return ()
    names = []
    for row in modal.select("table tbody tr"):
        cell = row.find("td")
        if cell is None:
            continue
        name = cell.get_text(" ", strip=True)
        if name:
            names.append(name)
    return tuple(names)


def parse_week(html: str) -> WeekPage:
    """Parse one week page into the days it covers and the selected items."""
    soup = make_soup(html)

    days: list[dt.date] = []
    for node in soup.select("#billoffare > li.date"):
        day = _parse_german_date(node.get_text(" ", strip=True))
        if day is not None and day not in days:
            days.append(day)

    records: list[MenuRecord] = []
    offered = 0
    for card in soup.select(f"[{CARD_ATTR}]"):
        payload = _card_payload(card)
        if payload is None:
            continue
        offered += 1

        classes = card.get("class") or []
        ordered_by_class = "ordered" in classes and "notordered" not in classes
        try:
            quantity = float(payload.get("QuantityOrdered") or 0)
        except (TypeError, ValueError):
            quantity = 0.0
        ordered = quantity > 0

        if ordered != ordered_by_class:
            log.warning(
                "card %s: QuantityOrdered=%s disagrees with CSS classes %s; "
                "trusting QuantityOrdered",
                card.get("id"),
                quantity,
                classes,
            )
        if not ordered:
            continue

        date_raw = (payload.get("BillofFareDate") or "")[:10]
        try:
            date = dt.date.fromisoformat(date_raw)
        except ValueError:
            log.warning("card %s has unparseable date %r, skipping", card.get("id"), date_raw)
            continue

        line = (payload.get("BillofFareDayGroupDescription") or "").strip() or "?"
        title = " ".join((payload.get("BillofFareItemDesc") or "").split())
        if not title:
            log.warning("card %s on %s has no title, skipping", card.get("id"), date)
            continue

        price_raw = payload.get("Price")
        price = None
        if isinstance(price_raw, (int, float)):
            price = f"{price_raw:.2f} EUR".replace(".", ",", 1)

        records.append(
            MenuRecord(
                date=date,
                line=line,
                title=title,
                price=price,
                allergens=_allergens_for(soup, card),
            )
        )

    if not days and records:
        days = sorted({r.date for r in records})

    records.sort(key=lambda r: (r.date, r.line))
    return WeekPage(
        week_start=days[0] if days else None,
        days=tuple(days),
        records=tuple(records),
        offered_count=offered,
    )


def parse_hidden_fields(html_or_soup) -> dict[str, str]:
    """All hidden inputs (``__VIEWSTATE`` and friends) of the WebForms page."""
    soup = html_or_soup if isinstance(html_or_soup, BeautifulSoup) else make_soup(html_or_soup)
    fields = {}
    for inp in soup.select("input[type=hidden]"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return fields


def parse_postback_target(href: str | None) -> tuple[str, str] | None:
    """Extract ``(target, argument)`` from a ``__doPostBack(...)`` href."""
    if not href:
        return None
    m = re.search(r"__doPostBack\((['\"])(.*?)\1\s*,\s*(['\"])(.*?)\3\)", href)
    if not m:
        return None
    return m.group(2), m.group(4)
