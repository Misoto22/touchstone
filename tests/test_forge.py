from __future__ import annotations

from tests.support.fake_gh import FakeGhExecutor
from touchstone.forge import Forge, PullState


def test_pull_state_preserves_head_and_check_conclusions() -> None:
    executor = FakeGhExecutor()
    executor.add_pull(
        number=12,
        head_sha="abc123",
        draft=True,
        checks=["SUCCESS", "FAILURE"],
    )

    pull = Forge("acme/widgets", executor).pull(12)

    assert pull == PullState(
        number=12,
        head_sha="abc123",
        branch="touchstone/run-1",
        draft=True,
        check_state="failure",
        merged_at=None,
        closed=False,
        created_at="2026-08-24T00:00:00Z",
        url="https://github.com/acme/widgets/pull/12",
    )


def test_pull_for_branch_returns_the_existing_pull() -> None:
    executor = FakeGhExecutor()
    executor.add_pull(number=12, head_sha="abc123", draft=False, checks=[])

    pull = Forge("acme/widgets", executor).pull_for_branch("touchstone/run-1")

    assert pull is not None
    assert pull.number == 12


def test_auto_merge_failure_returns_the_error() -> None:
    executor = FakeGhExecutor()
    executor.fail_next("pr merge", stderr="auto-merge is disabled")

    result = Forge("acme/widgets", executor).arm_auto_merge(12)

    assert (result.ok, result.detail) == (False, "auto-merge is disabled")


def test_repository_info_uses_the_current_github_api_shape() -> None:
    info = Forge("acme/widgets", FakeGhExecutor()).repository_info()

    assert info == {
        "nameWithOwner": "acme/widgets",
        "defaultBranchRef": {"name": "trunk"},
        "autoMergeAllowed": True,
    }


def test_branch_protection_uses_the_current_github_api_shape() -> None:
    assert Forge("acme/widgets", FakeGhExecutor()).branch_protection("trunk") is True
