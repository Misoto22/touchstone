from __future__ import annotations

from tests.support.fake_gh import FakeGhExecutor
from touchstone.forge import Forge, ForgeUnavailable, PullState


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


def test_malformed_github_collections_are_treated_as_unavailable() -> None:
    executor = FakeGhExecutor()
    executor.responses["pr list"] = '{"not": "a list"}'
    executor.responses["run list"] = '{"not": "a list"}'

    forge = Forge("acme/widgets", executor)

    assert forge.open_pulls("touchstone:audit", include_drafts=False) is None
    assert forge.latest_run("ci.yml", branch="trunk") == "unknown"


def test_malformed_pull_list_member_is_not_treated_as_an_empty_slot() -> None:
    executor = FakeGhExecutor()
    executor.responses["pr list"] = '[{"number": "12", "isDraft": false}]'

    assert (
        Forge("acme/widgets", executor).open_pulls("touchstone:audit", include_drafts=False) is None
    )


def test_malformed_pull_payload_is_treated_as_unavailable() -> None:
    executor = FakeGhExecutor()
    executor.pulls[12] = {"number": "not-an-integer"}

    assert Forge("acme/widgets", executor).pull(12) is None


def test_unavailable_branch_lookup_is_distinct_from_no_pull() -> None:
    executor = FakeGhExecutor()
    executor.responses["pr list"] = '{"not": "a list"}'

    import pytest

    with pytest.raises(ForgeUnavailable):
        Forge("acme/widgets", executor).pull_for_branch("touchstone/run-1")
