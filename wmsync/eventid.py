"""Deterministic Google Calendar event IDs derived from the menu slot.

Google requires event IDs to use base32hex (``a``-``v`` and ``0``-``9``),
length 5-1024. We hash the *slot* -- date plus menu line -- and never the food
name, so changing an order updates the existing event instead of creating a
second one.
"""

from __future__ import annotations

import base64
import hashlib

# NOTE: base32hex stops at "v", so a literal "wm" prefix is not a legal event
# ID. "vm" is the closest in-alphabet stand-in; events are additionally tagged
# via extendedProperties.private for unambiguous recognition.
ID_PREFIX = "vm"
# 26 base32hex chars = 130 bits of the digest; far beyond collision concerns
# and short enough to stay readable in the calendar API.
_DIGEST_CHARS = 26

_ALLOWED = set("abcdefghijklmnopqrstuv0123456789")


def slot_key(date, line: str) -> str:
    """The stable identity of a menu slot: ``2026-09-01|Menue 1``."""
    return f"{date.isoformat()}|{line}"


def event_id(slot: str, prefix: str = ID_PREFIX) -> str:
    """Map a slot key to a stable, valid Google Calendar event ID."""
    digest = hashlib.sha256(slot.encode("utf-8")).digest()
    # b32hexencode emits exactly the 0-9A-V alphabet Google wants; lowercasing
    # keeps it inside a-v/0-9 with no character left to remap.
    encoded = base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()
    candidate = f"{prefix}{encoded[:_DIGEST_CHARS]}"
    assert set(candidate) <= _ALLOWED, candidate
    assert 5 <= len(candidate) <= 1024, candidate
    return candidate


def is_valid_event_id(value: str) -> bool:
    return 5 <= len(value) <= 1024 and set(value) <= _ALLOWED
