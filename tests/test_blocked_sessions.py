"""A session that cannot act is not a session that found nothing.

For six hours the loop fired on schedule, spent seven minutes and 122k tokens
per run, located a real defect each time, wrote nothing, and recorded `clean`.
The host forbids unprivileged uid mapping, so bubblewrap could not bring up
loopback inside its namespace, the sandbox never started, and every file write
was refused — while `codex exec` went on exiting 0.

Six identical ledger entries saying all was well.
"""

from __future__ import annotations

from touchstone.engines.base import blocked_reason

# The transcript the real failure produced, trimmed to the lines that matter.
REAL_TRANSCRIPT = """
collab: Wait
ERROR codex_core::tools::router: error=Exit code: 1
Output:
Failed to write file /srv/touchstone/state/diag-worktree/src/kioku/core/serialization.py
apply patch
Failed to write file /srv/touchstone/state/diag-worktree/.audit-finding.json
codex
Blocked by workspace infrastructure. Every shell and file-write attempt failed with:
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`
No repository files were changed, tests could not run.
"""


def test_the_transcript_that_fooled_the_loop_is_recognised() -> None:
    assert blocked_reason(REAL_TRANSCRIPT) is not None


def test_an_ordinary_clean_run_is_not_mistaken_for_a_blocked_one() -> None:
    """A false positive costs one run recorded as held; a false negative costs
    an unbounded number recorded as clean. The asymmetry is why the markers are
    generous — but not so generous that ordinary prose trips them."""
    ordinary = (
        "Read docs/engineering/findings.md. Every row with Loop = yes is either "
        "already proposed or frozen pending a decision. No new structural defect "
        "beyond what the ledger records. Wrote no finding file."
    )
    assert blocked_reason(ordinary) is None


def test_a_diagnostic_no_ordinary_prose_produces_stands_alone() -> None:
    """These name the machinery, not the subject matter."""
    for fragment in (
        "bwrap: loopback: Failed RTM_NEWADDR",
        "Failed to write file /tmp/x",
        "Blocked by workspace infrastructure",
    ):
        assert blocked_reason(fragment) is not None, fragment


def test_a_phrase_the_repository_might_contain_needs_corroboration() -> None:
    """`Operation not permitted` is ordinary text in a diff about permissions.

    The first version matched it alone, which would have called a perfectly
    good review of an auth module blocked — and kioku's own queue has exactly
    such a row waiting. Alone it means nothing; beside a namespace or uid_map
    failure it is a machine refusing the engine.
    """
    assert blocked_reason("returns 403 with 'Operation not permitted' as the detail") is None
    assert blocked_reason("the test asserts EPERM / Operation not permitted") is None

    assert blocked_reason("write /proc/self/uid_map: Operation not permitted") is not None
    assert blocked_reason("unshare namespace: Operation not permitted") is not None


def test_the_reason_is_never_a_slice_of_the_transcript() -> None:
    """It travels into notes, the event log and the ledger, and those are
    packaged into the hosted state snapshot. The transcript stays on disk."""
    import inspect

    from touchstone.engines import base

    reason = blocked_reason("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted")
    assert reason is not None
    assert "bwrap" not in reason and "RTM_NEWADDR" not in reason

    catalogue = inspect.getsource(base)
    assert reason in catalogue, "the reason should be one of this module's own phrases"


def test_a_blocked_session_is_not_ok_whatever_the_exit_code_said() -> None:
    """This is the whole fix. `codex exec` exited 0 after refusing every write,
    and `ok` was read straight off that exit code."""
    import inspect

    from touchstone.engines import codex

    source = inspect.getsource(codex.CodexEngine._session)
    assert "blocked is None" in source, "ok is not conditioned on blocked"


def test_a_blocked_session_is_recorded_as_held_not_clean() -> None:
    import inspect

    from touchstone.nodes import audit

    source = inspect.getsource(audit.run)
    blocked_at = source.index("session.blocked")
    clean_at = source.index("_clean")
    assert blocked_at < clean_at, "the clean path is reached before blocked is checked"
    assert '"outcome": "held"' in source[blocked_at:clean_at]


def test_the_transcript_is_kept_so_a_silent_run_can_be_explained() -> None:
    """The first time this happened there was no record at all: the ledger said
    `no finding file written` and the reasoning behind it had been discarded."""
    import inspect

    from touchstone.engines import claude, codex

    assert "keep(" in inspect.getsource(codex.CodexEngine.author)
    assert "keep(" in inspect.getsource(codex.CodexEngine.review)
    assert "keep(" in inspect.getsource(claude.ClaudeEngine.author)


def test_the_sandbox_is_a_setting_with_the_safe_default() -> None:
    """Weakening it is a decision someone makes, never one the code falls back
    to on its own."""
    import dataclasses

    from touchstone.config import EngineConfig

    # Read the dataclass field, not the class attribute: with `slots=True` the
    # default never becomes one, so `EngineConfig.sandbox` raises rather than
    # answering — a test that asserted on it was testing the wrong surface.
    default = {f.name: f.default for f in dataclasses.fields(EngineConfig)}["sandbox"]
    assert default == "workspace-write"


def test_the_sandbox_key_is_accepted_by_the_validator() -> None:
    """The setting this change advertises has to be settable.

    `_validate()` runs before the value is read, so a key missing from the
    engine allowlist raises `unknown configuration key engine.sandbox` — the
    documented workaround would have failed on exactly the hosts it was written
    for, leaving an undocumented environment variable as the only way in.
    """
    from touchstone.config import _ENGINE

    assert "sandbox" in _ENGINE
