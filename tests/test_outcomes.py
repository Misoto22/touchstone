from __future__ import annotations

import pytest

from touchstone.outcomes import ChangeState, ResumeDecision, RunOutcome, RunResult


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
