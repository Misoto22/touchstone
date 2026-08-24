from __future__ import annotations

import json

import pytest

from touchstone.nodes.audit import parse_finding
from touchstone.nodes.review import parse_review


@pytest.mark.parametrize("raw", ["", "not json", '{"status":"unexpected"}'])
def test_invalid_finding_is_inconclusive(raw: str) -> None:
    finding = parse_finding(raw)

    assert finding.status == "inconclusive"
    assert finding.detail


def test_proposed_finding_requires_every_publication_field() -> None:
    finding = parse_finding('{"status":"proposed","risk":"low","title":"Missing body"}')

    assert finding.status == "inconclusive"
    assert "commit_subject" in finding.detail


def test_valid_finding_is_normalized_without_extra_fields() -> None:
    finding = parse_finding(
        json.dumps(
            {
                "status": "proposed",
                "risk": "low",
                "title": "Configuration drift",
                "commit_subject": "fix: prevent configuration drift",
                "summary": "Keeps one source of truth.",
                "rationale": "The duplicated value can drift.",
            }
        )
    )

    assert finding.to_state()["status"] == "proposed"
    assert finding.to_state()["risk"] == "low"


@pytest.mark.parametrize(
    "raw",
    ["", "not json", '{"verdict":"approve"}', '{"verdict":"maybe","reason":"x"}'],
)
def test_invalid_review_is_inconclusive(raw: str) -> None:
    assert parse_review(raw).status == "inconclusive"


def test_valid_review_preserves_the_explicit_verdict() -> None:
    review = parse_review('{"verdict":"approve","reason":"focused regression coverage"}')

    assert (review.status, review.verdict, review.reason) == (
        "valid",
        "approve",
        "focused regression coverage",
    )
