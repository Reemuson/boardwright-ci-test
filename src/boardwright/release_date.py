# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""The authoritative release date.

A release has exactly one date. ``release --prepare`` fixes it once (the UTC
calendar date at preparation, unless one is given) and records it in the
ledger entry, the CHANGELOG heading and ``.boardwright/release.env``. Drawings
read it from ``release.env`` (``RELEASE_DATE``, ``RELEASE_DATE_NUM``), never
from a commit timestamp, so the held candidate, the approved run (which takes
the reviewed date) and the tagged publish build all show the same date
whatever the commit times or the host's time zone.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from .errors import BoardwrightError

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Fixed English abbreviations: strftime("%b") follows the host locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def utc_today(now: datetime | None = None) -> date:
    """The UTC calendar date of ``now`` (default: the current instant)."""

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise BoardwrightError("Release dates are taken from an aware timestamp, not a naive local time.")
    return moment.astimezone(timezone.utc).date()


def parse_release_date(text: str) -> date:
    value = str(text or "").strip()
    if not ISO_DATE_RE.match(value):
        raise BoardwrightError(f"Release date must be YYYY-MM-DD, got {value!r}.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BoardwrightError(f"Release date {value!r} is not a calendar date.") from exc


def resolve_release_date(text: str = "", now: datetime | None = None) -> date:
    """The given date, or the UTC date now when none is given."""

    return parse_release_date(text) if str(text or "").strip() else utc_today(now)


def display_date(value: date) -> str:
    """``RELEASE_DATE`` text, e.g. ``19-Nov-2026`` (same shape as the old git fallback)."""

    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year:04d}"


def release_env_lines(value: date) -> list[str]:
    return [f"RELEASE_DATE={display_date(value)}", f"RELEASE_DATE_NUM={value.isoformat()}"]
