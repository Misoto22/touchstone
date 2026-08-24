from __future__ import annotations

import pytest

from touchstone.outcomes import (
    ChangeState,
    ResumeDecision,
    RunOutcome,
    RunResult,
    from_legacy_outcome,
)


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [
        (RunOutcome.COMPLETED, 0),
        (RunOutcome.NO_CHANGE, 0),
        (RunOutcome.REHEARSED, 0),
        (RunOutcome.BLOCKED, 3),
        (RunOutcome.FAILED, 1),
    ],
)
def test_run_outcomes_have_stable_exit_codes(outcome: RunOutcome, exit_code: int) -> None:
    assert RunResult(outcome=outcome).exit_code == exit_code


def test_run_and_change_state_are_separate_machine_contracts() -> None:
    result = RunResult(
        outcome=RunOutcome.COMPLETED,
        lifecycle=ChangeState.AWAITING_CHECKS,
        candidate_id="candidate-1",
        pr_number=12,
    )

    assert result.to_dict() == {
        "version": 1,
        "outcome": "completed",
        "lifecycle": "awaiting_checks",
        "candidate_id": "candidate-1",
        "pr_number": 12,
        "partial": False,
        "retryable": False,
        "exit_code": 0,
    }


def test_resume_decisions_are_bounded() -> None:
    assert {decision.value for decision in ResumeDecision} == {
        "approve",
        "close",
        "reanalyze",
    }


def test_a_parked_thread_does_not_launder_a_failed_publication() -> None:
    """A pull request that exists parks the thread; that is not a successful run."""
    result = from_legacy_outcome("failed", paused=True, pr=487, partial=True)

    assert result.outcome is RunOutcome.FAILED
    assert result.exit_code == 1
    assert result.partial is True
    assert result.reason_code == "partial-publication"
    assert result.lifecycle is ChangeState.FAILED
    assert result.pr_number == 487


def test_a_partial_write_is_a_failure_whatever_the_graph_did_next() -> None:
    for value in ("failed", "held", "awaiting_human", "awaiting_checks", "clean"):
        for paused in (False, True):
            result = from_legacy_outcome(value, paused=paused, partial=True)
            assert result.exit_code == 1, (value, paused)
            assert result.partial is True


def test_a_blocked_run_keeps_its_exit_code_when_the_thread_parked() -> None:
    assert from_legacy_outcome("held", paused=True).exit_code == 3
    assert from_legacy_outcome("blocked", paused=True).exit_code == 3


def test_an_ordinary_park_is_still_a_completed_run() -> None:
    result = from_legacy_outcome("awaiting_human", paused=True, pr=12)

    assert result.outcome is RunOutcome.COMPLETED
    assert result.exit_code == 0
    assert result.lifecycle is ChangeState.AWAITING_HUMAN
    assert result.partial is False


def test_a_rehearsal_is_never_reported_as_a_failure() -> None:
    assert from_legacy_outcome("failed", dry_run=True, partial=True).exit_code == 0
