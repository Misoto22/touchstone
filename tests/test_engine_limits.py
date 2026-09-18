"""A spent plan is not a failed session, and it says when it will recover.

kioku's host spent three days in September 2026 waking every hour and
recording `the codex session failed`, while the transcript it kept said the
usage limit would reset on a named day.
"""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import pytest

from touchstone.config import EngineConfig
from touchstone.engines.base import failed_limit
from touchstone.engines.codex import CodexEngine
from touchstone.engines.limits import DEFAULT_PAUSE, usage_limit
from touchstone.execution.base import Result

# The line the real failure produced, verbatim.
REAL_TRANSCRIPT = (
    "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    "to purchase more credits or try again at Sep 19th, 2026 8:59 PM.\n"
)
NOW = dt.datetime(2026, 9, 17, 22, 5, tzinfo=dt.UTC)


@pytest.fixture
def utc_clock(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """The CLI formats the reset in its local zone; pin that zone."""

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_real_limit_is_read_with_its_reset_time(utc_clock) -> None:  # type: ignore[no-untyped-def]
    limit = usage_limit(REAL_TRANSCRIPT, now=NOW)

    assert limit is not None
    assert limit.until == dt.datetime(2026, 9, 19, 20, 59, tzinfo=dt.UTC)
    assert limit.reason == "usage limit was reached"


def test_a_reset_later_today_is_read_as_today(utc_clock) -> None:  # type: ignore[no-untyped-def]
    transcript = "ERROR: You've hit your usage limit. Try again at 11:30 PM.\n"

    limit = usage_limit(transcript, now=NOW)

    assert limit is not None
    assert limit.until == dt.datetime(2026, 9, 17, 23, 30, tzinfo=dt.UTC)


def test_a_clock_time_already_past_means_tomorrow(utc_clock) -> None:  # type: ignore[no-untyped-def]
    transcript = "ERROR: You've hit your usage limit. Try again at 9:00 PM.\n"

    limit = usage_limit(transcript, now=NOW)

    assert limit is not None
    assert limit.until == dt.datetime(2026, 9, 18, 21, 0, tzinfo=dt.UTC)


def test_a_limit_without_a_reset_time_pauses_for_the_default() -> None:
    limit = usage_limit("ERROR: You've hit your usage limit. Try again later.\n", now=NOW)

    assert limit is not None
    assert limit.until == NOW + DEFAULT_PAUSE


def test_a_reset_time_is_held_inside_sane_bounds(utc_clock) -> None:  # type: ignore[no-untyped-def]
    """A misread zone must not pause an engine for a month or for no time."""

    far = "ERROR: You've hit your usage limit. Try again at Dec 25th, 2026 9:00 AM.\n"
    past = "ERROR: You've hit your usage limit. Try again at Sep 1st, 2026 9:00 AM.\n"

    assert usage_limit(far, now=NOW).until == NOW + dt.timedelta(days=8)  # type: ignore[union-attr]
    assert usage_limit(past, now=NOW).until == NOW + dt.timedelta(minutes=15)  # type: ignore[union-attr]


def test_prose_about_usage_limits_is_not_a_limit() -> None:
    """The transcript quotes the repository, which can discuss rate limits."""

    prose = (
        'The handler returns 429 and the message "You\'ve hit your usage limit" '
        "when a tenant exceeds its plan; try again at Sep 19th, 2026 8:59 PM."
    )

    assert usage_limit(prose, now=NOW) is None


def test_a_session_that_succeeded_is_never_read_for_a_limit() -> None:
    assert failed_limit(True, REAL_TRANSCRIPT) is None
    assert failed_limit(False, REAL_TRANSCRIPT) is not None


def test_the_reason_is_never_a_slice_of_the_transcript() -> None:
    limit = usage_limit(REAL_TRANSCRIPT, now=NOW)

    assert limit is not None
    assert "chatgpt.com" not in limit.reason and "credits" not in limit.reason


class _Executor:
    replaces_environment = True
    where = "local"

    def __init__(self, result: Result) -> None:
        self._result = result

    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        return self._result


def _engine(tmp_path, result: Result) -> CodexEngine:  # type: ignore[no-untyped-def]
    config = SimpleNamespace(state_dir=str(tmp_path), engine=EngineConfig(name="codex"))
    return CodexEngine(config, _Executor(result))


def test_codex_reports_the_limit_its_failed_session_met(tmp_path) -> None:  # type: ignore[no-untyped-def]
    session = _engine(tmp_path, Result(1, "", REAL_TRANSCRIPT)).author(
        "brief", worktree=str(tmp_path), denied=()
    )

    assert session.ok is False
    assert session.limited is not None
    assert (tmp_path / "engine-author.log").read_text(encoding="utf-8") == REAL_TRANSCRIPT


def test_codex_reports_no_limit_for_an_ordinary_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    session = _engine(tmp_path, Result(1, "", "error: connection reset\n")).author(
        "brief", worktree=str(tmp_path), denied=()
    )

    assert session.ok is False
    assert session.limited is None
