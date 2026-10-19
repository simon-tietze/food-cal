# WebMenü → Google Calendar sync

Scrapes the lunch items **you have actually ordered** from a WebMenü instance
and mirrors each one into a Google Calendar as an idempotent all-day event.

## How the site behaves

Findings from investigating `https://wiaf.webmenue.info/BillofFare.aspx`:

| Question | Answer |
| --- | --- |
| Login | `GET BillofFare.aspx` 302s to `login.aspx?err=5`. Post the page's `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION` back together with `__EVENTTARGET=ctl00$MainContent$btnlogin`, `ctl00$MainContent$txtLogin`, `ctl00$MainContent$txtPassword`. The submit control is an `<a>` firing `__doPostBack`, so there is no submit button name to send. Success redirects to `BillofFare.aspx` and sets `WebMenuSessionID` (HttpOnly) plus a `Webmenu` preferences cookie. |
| Week representation | One `<ul id="billoffare">` per week: `li.date` header cells for the five workdays, then one `li.card` per (menu line × day). Each card carries `data-ntc-billoffare-element`, a **base64 blob of JSON** with `BillofFareDate`, `BillofFareDayGroupDescription` (the menu line), `BillofFareItemDesc`, `Price` and `QuantityOrdered`. Allergens live in a per-card modal `#modal-billoffareitemdetails-<card id>` under the heading *Unverträglichkeiten*; additive codes (`a1,c,g`) are inline in the title. |
| Paging | `a.week-next` → `__doPostBack('ctl00$MainContent$ctl03','')`. Post it with the current hidden fields plus the datepicker value `ctl00$MainContent$txtDate`. |
| End of the plan | **The next-week arrow is never disabled** — it pages into empty weeks indefinitely. The reliable stop condition is a week that renders zero menu cards, which is what this tool uses (bounded by `--weeks`). At the time of writing the plan ended after the week of 28.09.2026. |
| Selected vs. offered | `QuantityOrdered > 0` in the card payload, corroborated by the `ordered` / `notordered` CSS class on the `li.card`. The parser trusts `QuantityOrdered` and logs a warning if the two disagree. Ordering checkboxes are `disabled` once the deadline has passed (`fristexpired`), so their state is *not* a reliable signal. |
| Multiple items per day | Structurally yes — each menu line (`Menü 1`, `Vegetarisch`, …) is an independent slot and could each be ordered. In the observed data exactly one line per day was ordered. The slot key is `date|line`, so several selections per day are handled naturally. |
| Empty days | Weeks with a public holiday render `li.card.empty.freeday` placeholders with an empty payload attribute; these are skipped. |

**`requests` vs. Playwright:** a plain `requests.Session` is enough and is what
this tool uses. Login and paging are ordinary WebForms postbacks, the viewstate
survives them, and all menu data is server-rendered into the HTML (indeed into
a single data attribute). No JavaScript is required to render or to page, so
Playwright would add a browser dependency for nothing.

## Layout

```
wmsync/parser.py    pure HTML → records (no network; unit tested via fixtures)
wmsync/scraper.py   login + week paging over requests.Session
wmsync/eventid.py   deterministic base32hex event IDs
wmsync/gcal.py      event construction, diffing, retries, writes
wmsync/cli.py       argument parsing, logging, exit codes
sync_menu.py        entry point
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Two secret files are needed. Neither is ever hardcoded; both are read by path
and both are in `.gitignore` (along with a blanket `*.json` rule).

| File | Default path | Override |
| --- | --- | --- |
| WebMenü login | `creds.json` | `WEBMENUE_CREDS` or `--creds` |
| Google service account key | `majestic-poetry-94110-ff3cbc4b22a6.json` | `GOOGLE_SA_KEY` or `--sa-key` |

`creds.json` is read flexibly: any of `user`/`username`/`login`/… plus any of
`pwd`/`password`/`passwort`/… is accepted.

The target calendar must be shared with the service account
(`food-cal@majestic-poetry-94110.iam.gserviceaccount.com`) with *Make changes
to events*. The calendar is always addressed by its explicit ID — a service
account has no `primary` calendar. Override with `--calendar-id` or
`GOOGLE_CALENDAR_ID`.

## Running

```bash
.venv/bin/python sync_menu.py --dry-run          # full scrape + diff, no writes
.venv/bin/python sync_menu.py --weeks 6 -v       # page 6 weeks forward, debug logs
.venv/bin/python sync_menu.py                    # sync
```

`--dry-run` exercises everything except the mutating calls: it logs in, pages
every week, parses, reads the existing calendar events and computes the real
diff. Only `insert`/`update`/`delete` are skipped.

Logs go to stdout; the process exits non-zero on any failure.

## Idempotency

Event IDs are `SHA-256(f"{date}|{line}")` → base32hex → lowercase → first 26
characters, prefixed with a constant. So the ID depends on the **slot**, never
on the food name: change your order and the existing event is updated in place
instead of a duplicate appearing.

> Note: Google's base32hex alphabet is `a`–`v` plus `0`–`9`, and `w` is outside
> it, so the literal prefix `wm` is not a legal event ID. The prefix used is
> **`vm`**. Recognition does not depend on it anyway — every event this tool
> writes is tagged `extendedProperties.private.generator = webmenue-gcal-sync`
> and carries its slot key, and that tag is what the calendar is queried by.

Sync semantics:

- `events.insert` first; a `409 Conflict` falls through to `events.update`.
- An update is only issued when a field actually differs, so an unchanged run
  performs no writes at all.
- A slot that disappears from the scrape has its event deleted. Deletion is
  scoped to `timeMin`/`timeMax` covering **only the days the run actually
  visited**, and further restricted to events carrying this tool's generator
  tag — an unrelated failure cannot touch other weeks or other events.
- `410 Gone` on delete is treated as success.
- Google API calls retry with exponential backoff on 5xx, 429 and rate-limit
  flavoured 403s. Auth and other 4xx errors are never retried.
- **A scrape returning zero selected items is a hard failure** and deletes
  nothing, because that almost always means a broken login or a changed DOM.
  Pass `--allow-empty` if you genuinely have no orders and want the calendar
  emptied.

Each run logs a summary line: `created=N updated=N unchanged=N deleted=N`.

## Scheduling (systemd)

Install for your user:

```bash
mkdir -p ~/.config/systemd/user
sed "s|/path/to/food-cal|$PWD|g" systemd/webmenue-sync.service \
  > ~/.config/systemd/user/webmenue-sync.service
cp systemd/webmenue-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now webmenue-sync.timer
```

Check on it:

```bash
systemctl --user list-timers webmenue-sync.timer
journalctl --user -u webmenue-sync.service -n 50
```

`systemctl --user` services stop when you log out unless lingering is enabled
(`sudo loginctl enable-linger $USER`). For an always-on machine, install the
units under `/etc/systemd/system/` instead and set `User=`/`Group=`.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

Covers the event-ID derivation (alphabet, determinism, slot-not-title
stability, collisions), the parser against saved HTML fixtures (a week with
orders, a week with none plus holiday placeholders, the login page) and the
diff/retry logic.
