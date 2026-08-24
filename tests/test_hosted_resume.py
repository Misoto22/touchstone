from __future__ import annotations

import pytest

from touchstone.hosted.runtime import CandidateIntegrityError, ResumeInput


def test_resume_input_accepts_only_exact_candidate_and_decision() -> None:
    resume = ResumeInput.from_environment(
        {
            "TOUCHSTONE_CANDIDATE_ID": "candidate-123",
            "TOUCHSTONE_DECISION": "approve",
        }
    )

    assert resume.candidate_id == "candidate-123"
    assert resume.decision == "approve"


@pytest.mark.parametrize("decision", ["merge", "yes", "APPROVE"])
def test_resume_input_rejects_unbounded_decisions(decision: str) -> None:
    with pytest.raises(CandidateIntegrityError, match="decision"):
        ResumeInput.from_environment(
            {"TOUCHSTONE_CANDIDATE_ID": "candidate-123", "TOUCHSTONE_DECISION": decision}
        )


def test_resume_input_requires_both_fields_and_bounds_candidate_id() -> None:
    with pytest.raises(CandidateIntegrityError, match="together"):
        ResumeInput.from_environment({"TOUCHSTONE_CANDIDATE_ID": "candidate-123"})
    with pytest.raises(CandidateIntegrityError, match="candidate"):
        ResumeInput.from_environment(
            {"TOUCHSTONE_CANDIDATE_ID": "x" * 129, "TOUCHSTONE_DECISION": "close"}
        )
