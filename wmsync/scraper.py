"""Fetching layer: log in to WebMenue and page forward week by week.

A plain ``requests.Session`` is sufficient (see README, "How the site
behaves"): login and paging are ordinary ASP.NET WebForms postbacks and the
menu data is fully present in the server-rendered HTML. No Playwright needed.
"""

from __future__ import annotations

import datetime as dt
import logging

import requests

from .models import MenuRecord, WeekPage
from .parser import (
    decode_page,
    make_soup,
    parse_hidden_fields,
    parse_postback_target,
    parse_week,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://wiaf.webmenue.info/"
BOF_PATH = "BillofFare.aspx"
LOGIN_BUTTON = "ctl00$MainContent$btnlogin"
USER_AGENT = "webmenue-gcal-sync/1.0 (+https://github.com/)"

# creds.json in the wild uses short keys; accept the obvious spellings rather
# than assuming one schema.
_USER_KEYS = ("user", "username", "login", "benutzer", "benutzername", "email")
_PASS_KEYS = ("pwd", "password", "pass", "passwort", "secret")


class ScrapeError(RuntimeError):
    """Raised when the site does not behave as the scraper expects."""


def extract_credentials(blob: dict) -> tuple[str, str]:
    """Pull username/password out of the credentials file, whatever it calls them."""
    lowered = {str(k).lower(): v for k, v in blob.items()}
    user = next((lowered[k] for k in _USER_KEYS if lowered.get(k)), None)
    password = next((lowered[k] for k in _PASS_KEYS if lowered.get(k)), None)
    if not user or not password:
        raise ScrapeError(
            "credentials file has no recognisable username/password keys "
            f"(found: {sorted(blob)})"
        )
    return str(user), str(password)


class WebMenueClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: int = 30):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    # -- plumbing ---------------------------------------------------------
    def _get(self, path: str) -> tuple[str, str]:
        r = self.session.get(self.base_url + path, timeout=self.timeout)
        r.raise_for_status()
        return decode_page(r.content), str(r.url)

    def _post(self, url: str, data: dict) -> tuple[str, str]:
        r = self.session.post(url, data=data, timeout=self.timeout)
        r.raise_for_status()
        return decode_page(r.content), str(r.url)

    # -- flow -------------------------------------------------------------
    def login(self, user: str, password: str) -> None:
        """Log in. Unauthenticated hits on BillofFare redirect to login.aspx."""
        html, url = self._get(BOF_PATH)
        soup = make_soup(html)
        if soup.select_one("#MainContent_txtPassword") is None:
            log.debug("already authenticated, no login form present")
            return

        form = parse_hidden_fields(soup)
        form.update(
            {
                "__EVENTTARGET": LOGIN_BUTTON,
                "__EVENTARGUMENT": "",
                "ctl00$MainContent$txtLogin": user,
                "ctl00$MainContent$txtPassword": password,
            }
        )
        html, url = self._post(url, form)
        soup = make_soup(html)
        if soup.select_one("#MainContent_txtPassword") is not None:
            raise ScrapeError(
                "login rejected: still on the login form after posting credentials"
            )
        log.info("logged in to %s as %s", self.base_url, user)

    def _week_pages(self, max_weeks: int):
        """Yield ``(html, WeekPage)`` starting at the current week."""
        html, _ = self._get(BOF_PATH)
        for index in range(max_weeks):
            page = parse_week(html)
            yield html, page

            if not page.has_menu:
                # The "next week" arrow is never disabled: it happily pages into
                # empty weeks forever. An empty week is the end of the plan.
                log.info("week starting %s has no menu; stopping", page.week_start)
                return
            if index == max_weeks - 1:
                return

            soup = make_soup(html)
            link = soup.select_one(".week-next")
            target = parse_postback_target(link.get("href") if link else None)
            if target is None:
                log.info("no 'next week' control on the page; stopping")
                return

            form = parse_hidden_fields(soup)
            form["__EVENTTARGET"], form["__EVENTARGUMENT"] = target
            picker = soup.select_one("#MainContent_txtDate")
            if picker is not None and picker.get("name"):
                form[picker["name"]] = picker.get("value", "")
            html, _ = self._post(self.base_url + BOF_PATH, form)

    def scrape(self, max_weeks: int = 4) -> tuple[list[MenuRecord], list[dt.date]]:
        """Return selected items plus every day the run actually looked at.

        The covered days come from the pages themselves, not from the records,
        so a week where nothing is ordered still bounds the deletion window.
        """
        records: list[MenuRecord] = []
        covered: list[dt.date] = []
        for _, page in self._week_pages(max_weeks):
            covered.extend(page.days)
            records.extend(page.records)
            log.info(
                "week %s: %d offered, %d selected",
                page.week_start.isoformat() if page.week_start else "?",
                page.offered_count,
                len(page.records),
            )
        records.sort(key=lambda r: (r.date, r.line))
        return records, sorted(set(covered))
