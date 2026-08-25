"""Auto-merge is per-Loop, gated six ways, and only where Verify is independent."""

from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.config import ConfigError, load
from touchstone.lifecycle import auto_merge_verdict


def _verdict(**overrides: object):  # type: ignore[no-untyped-def]
    conditions: dict[str, object] = {
        "requested": True,
        "independently_verified": True,
        "risk": "low",
        "verdict": "approve",
        "draft": False,
        "gates_passed": True,
        "merge_allowed": True,
        "required_checks_declared": True,
        "protected_branch": True,
    }
    conditions.update(overrides)
    return auto_merge_verdict(**conditions)  # type: ignore[arg-type]


def test_every_condition_met_arms_the_merge() -> None:
    result = _verdict()

    assert result.armed is True
    assert result.blocked is False
    assert result.unmet == ()


def test_a_loop_that_does_not_ask_is_not_armed_and_not_blocked() -> None:
    result = _verdict(requested=False)

    assert result.armed is False
    assert result.blocked is False


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"risk": "medium"}, "risk"),
        ({"verdict": "reject"}, "review"),
        ({"draft": True}, "draft"),
        ({"gates_passed": False}, "validation gate"),
        ({"merge_allowed": False}, "allow auto-merge"),
        ({"required_checks_declared": False}, "required workflow"),
        ({"protected_branch": False}, "protection"),
    ],
)
def test_each_unmet_condition_refuses_and_says_which(
    override: dict[str, object], fragment: str
) -> None:
    result = _verdict(**override)

    assert result.armed is False
    assert fragment in result.reason


def test_the_verdict_names_every_unmet_condition_not_just_the_first() -> None:
    result = _verdict(risk="high", merge_allowed=False, protected_branch=False)

    assert len(result.unmet) == 3


def test_a_backend_without_an_independent_verify_stage_blocks() -> None:
    result = _verdict(independently_verified=False)

    assert result.armed is False
    # Blocked, not silently downgraded: a Loop configured to merge unattended
    # and quietly opening pull requests instead is a configuration that lies.
    assert result.blocked is True
    assert "policy-unsupported" in result.reason


def test_a_backend_without_verify_blocks_before_anything_else_is_examined() -> None:
    result = _verdict(independently_verified=False, risk="high", merge_allowed=False)

    assert result.blocked is True
    assert len(result.unmet) == 1


def test_auto_merge_is_configured_per_loop(tmp_path: Path) -> None:
    path = tmp_path / "touchstone.toml"
    path.write_text(
        "\n".join(
            [
                "version = 1",
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
                "[engine]",
                'name = "codex"',
                'model = "m"',
                "[loop.tidy]",
                'brief = "builtin:code-audit"',
                'label = "touchstone:tidy"',
                "auto_merge = true",
                "[loop.refactor]",
                'brief = "builtin:code-audit"',
                'label = "touchstone:refactor"',
            ]
        ),
        encoding="utf-8",
    )

    config = load(path)

    assert config.loop("tidy").auto_merge is True
    assert config.loop("refactor").auto_merge is False


def test_auto_merge_must_be_a_boolean(tmp_path: Path) -> None:
    path = tmp_path / "touchstone.toml"
    path.write_text(
        "\n".join(
            [
                "version = 1",
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
                "[engine]",
                'name = "codex"',
                'model = "m"',
                "[loop.tidy]",
                'brief = "builtin:code-audit"',
                'label = "touchstone:tidy"',
                'auto_merge = "yes"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"loop\.tidy\.auto_merge"):
        load(path)


def test_the_forge_is_asked_to_merge_only_when_every_condition_holds() -> None:
    from types import SimpleNamespace

    from touchstone.forge import OperationResult
    from touchstone.lifecycle import PublicationRequest, RepositoryLifecycle

    class ForgeStub:
        def __init__(self, *, allowed: bool, protected: bool) -> None:
            self.allowed = allowed
            self.protected = protected
            self.armed: list[int] = []

        def repository_info(self) -> dict[str, object]:
            return {"autoMergeAllowed": self.allowed}

        def branch_protection(self, _branch: str) -> bool:
            return self.protected

        def arm_auto_merge(self, number: int) -> OperationResult:
            self.armed.append(number)
            return OperationResult(True, "")

    def _request(**overrides: object) -> PublicationRequest:
        fields: dict[str, object] = {
            "finding_id": "f",
            "loop": "code",
            "branch": "touchstone/a",
            "worktree": Path("."),
            "base": "main",
            "label": "touchstone:audit",
            "escalation_label": "touchstone:needs-review",
            "risk": "low",
            "verdict": "approve",
            "title": "T",
            "commit_subject": "fix: t",
            "summary": "",
            "rationale": "",
            "review_reason": "",
            "auto_merge": True,
            "independently_verified": True,
            "required_checks_declared": True,
        }
        fields.update(overrides)
        return PublicationRequest(**fields)  # type: ignore[arg-type]

    forge = ForgeStub(allowed=True, protected=True)
    lifecycle = RepositoryLifecycle(
        forge,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        reap_after_hours=6,
        executor=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert lifecycle._arm_auto_merge(_request(), 7) != ""
    assert forge.armed == [7]

    # A repository that does not allow auto-merge is not asked to perform one.
    refuses = ForgeStub(allowed=False, protected=True)
    refusing = RepositoryLifecycle(
        refuses,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        reap_after_hours=6,
        executor=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert refusing._arm_auto_merge(_request(), 7) == ""
    assert refuses.armed == []

    # An unprotected base branch is not asked either.
    unprotected = ForgeStub(allowed=True, protected=False)
    lax = RepositoryLifecycle(
        unprotected,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        reap_after_hours=6,
        executor=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert lax._arm_auto_merge(_request(), 7) == ""
    assert unprotected.armed == []


def test_a_local_run_refuses_to_publish_a_loop_that_asks_to_auto_merge() -> None:
    from touchstone.lifecycle import auto_merge_unsupported

    # `validation_required` is True for a local run, so the publish node passes
    # independently_verified=False and the configuration is refused before any
    # branch is pushed.
    assert auto_merge_unsupported(requested=True, independently_verified=False) != ""
    assert auto_merge_unsupported(requested=True, independently_verified=True) == ""
    assert auto_merge_unsupported(requested=False, independently_verified=False) == ""
