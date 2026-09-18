"""How an engine says its plan is exhausted, and until when.

kioku's host spent three days in September 2026 waking every hour, starting a
Codex session that failed within a minute, and recording `the codex session
failed`. The transcript it kept said why in plain words — the plan's usage
limit was reached and would reset on a named day — and nothing read it, so the
loop went on paying for a session the provider had already said it would
refuse until then.

Recognised only after a session has already failed. A transcript quotes the
repository, and a repository can discuss usage limits as its subject matter;
reading these markers from a session that exited cleanly would let that prose
pause the engine.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

#: The line `codex exec` prints when the plan behind its credential is spent.
#: Anchored to the start of a line and to the CLI's own `ERROR:` prefix, which
#: model prose and quoted source do not produce.
_CODEX_LIMIT = re.compile(r"^ERROR: You've hit your usage limit\b", re.MULTILINE)
_RETRY_AT = re.compile(r"try again at (?P<when>[^\n]+?)\.?[ \t]*$", re.IGNORECASE | re.MULTILINE)
_ORDINAL = re.compile(r"(\d)(?:st|nd|rd|th)\b")

#: The pause when the provider names no reset time: long enough that an
#: hourly wake does not immediately spend another session, short enough that a
#: limit lifted early costs at most one skipped period.
DEFAULT_PAUSE = dt.timedelta(hours=1)
#: Bounds on a parsed reset time. The CLI formats it in its own local zone,
#: which this process can only assume matches its own; the bounds keep a wrong
#: assumption from pausing an engine for longer than a weekly plan window.
_SHORTEST = dt.timedelta(minutes=15)
_LONGEST = dt.timedelta(days=8)


@dataclass(frozen=True, slots=True)
class UsageLimit:
    """An engine refusing work until `until`, in words safe to persist."""

    #: A phrase of this module's own, never a slice of the transcript.
    reason: str
    until: dt.datetime


def usage_limit(transcript: str, *, now: dt.datetime) -> UsageLimit | None:
    """The limit a failed session reported, or `None` when it reported none."""

    if now.tzinfo is None:
        raise ValueError("usage limit evaluation requires an aware datetime")
    if not _CODEX_LIMIT.search(transcript):
        return None
    reset = None
    for match in _RETRY_AT.finditer(transcript):
        reset = _reset_time(match.group("when"), now)
        if reset is not None:
            break
    if reset is None:
        until = now + DEFAULT_PAUSE
    else:
        until = min(max(reset, now + _SHORTEST), now + _LONGEST)
    return UsageLimit("usage limit was reached", until.astimezone(dt.UTC).replace(microsecond=0))


def _reset_time(text: str, now: dt.datetime) -> dt.datetime | None:
    """Read `Sep 19th, 2026 8:59 PM` or, for a reset later today, `8:59 PM`."""

    cleaned = _ORDINAL.sub(r"\1", text.strip())
    local = now.astimezone()
    try:
        parsed = dt.datetime.strptime(cleaned, "%b %d, %Y %I:%M %p")
    except ValueError:
        pass
    else:
        return parsed.replace(tzinfo=local.tzinfo).astimezone(dt.UTC)
    try:
        clock = dt.datetime.strptime(cleaned, "%I:%M %p")
    except ValueError:
        return None
    today = local.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if today <= local:
        today += dt.timedelta(days=1)
    return today.astimezone(dt.UTC)


__all__ = ["DEFAULT_PAUSE", "UsageLimit", "usage_limit"]
