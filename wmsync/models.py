"""Data types shared between the scraping and calendar layers."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MenuRecord:
    """One *selected* menu item for one day.

    ``date``/``line``/``title`` are the contract the calendar layer relies on;
    ``line`` is the stable slot identifier (the WebMenue "day group" name, e.g.
    ``Menue 1`` or ``Vegetarisch``) and never changes when the order changes.
    The remaining fields are optional extras used to build the description.
    """

    date: dt.date
    line: str
    title: str
    price: str | None = None
    allergens: tuple[str, ...] = ()

    @property
    def slot_key(self) -> str:
        return f"{self.date.isoformat()}|{self.line}"

    def as_dict(self) -> dict:
        return {"date": self.date, "line": self.line, "title": self.title}


@dataclass(frozen=True)
class WeekPage:
    """A parsed week of the Speiseplan."""

    week_start: dt.date | None
    days: tuple[dt.date, ...] = ()
    records: tuple[MenuRecord, ...] = ()
    offered_count: int = 0

    @property
    def has_menu(self) -> bool:
        """True when the week shows any menu at all (ordered or not)."""
        return self.offered_count > 0
