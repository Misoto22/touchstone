"""Which identity authors a published commit, and who is credited beside it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.config import ConfigError, GitConfig, load

BOT = ("acme[bot]", "1+acme[bot]@users.noreply.github.com")
OPERATOR = ("Operator", "7+operator@users.noreply.github.com")


def _git(**overrides: object) -> GitConfig:
    fields: dict[str, object] = {
        "author_name": BOT[0],
        "author_email": BOT[1],
        "operator_name": OPERATOR[0],
        "operator_email": OPERATOR[1],
    }
    fields.update(overrides)
    return GitConfig(**fields)  # type: ignore[arg-type]


def test_the_bot_authors_by_default_and_the_operator_is_credited() -> None:
    assert _git().identities() == (BOT, OPERATOR)


def test_naming_the_operator_as_author_swaps_the_two_identities() -> None:
    assert _git(author="operator").identities() == (OPERATOR, BOT)


def test_a_hosted_bot_identity_replaces_the_configured_one() -> None:
    hosted = ("widgets[bot]", "9+widgets[bot]@users.noreply.github.com")
    assert _git().identities(bot=hosted) == (hosted, OPERATOR)
    assert _git(author="operator").identities(bot=hosted) == (OPERATOR, hosted)


def test_without_an_operator_nobody_is_credited() -> None:
    author, coauthor = GitConfig(author_name=BOT[0], author_email=BOT[1]).identities()
    assert author == BOT
    assert coauthor is None


def test_an_operator_author_without_an_operator_identity_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"git\.operator_name"):
        GitConfig(author="operator")


def test_operator_fields_must_be_set_together() -> None:
    with pytest.raises(ConfigError, match=r"git\.operator_name and git\.operator_email"):
        GitConfig(operator_name=OPERATOR[0])


def test_an_unknown_author_choice_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"git\.author"):
        GitConfig(author="maintainer")  # type: ignore[arg-type]


def test_the_configuration_file_carries_the_choice(tmp_path: Path) -> None:
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
                "[git]",
                'author = "operator"',
                f'author_name = "{BOT[0]}"',
                f'author_email = "{BOT[1]}"',
                f'operator_name = "{OPERATOR[0]}"',
                f'operator_email = "{OPERATOR[1]}"',
                "[loop.code]",
                'brief = "builtin:code-audit"',
                'label = "touchstone:audit"',
            ]
        ),
        encoding="utf-8",
    )
    assert load(path).git.identities() == (OPERATOR, BOT)


def test_the_commit_credits_the_second_identity() -> None:
    from touchstone import lifecycle

    request = SimpleNamespace(
        coauthor_name=OPERATOR[0],
        coauthor_email=OPERATOR[1],
    )
    assert lifecycle._coauthor_trailer(request) == (  # type: ignore[arg-type]
        f"Co-Authored-By: {OPERATOR[0]} <{OPERATOR[1]}>"
    )


def test_no_second_identity_means_no_trailer() -> None:
    from touchstone import lifecycle

    request = SimpleNamespace(coauthor_name=None, coauthor_email=None)
    assert lifecycle._coauthor_trailer(request) == ""  # type: ignore[arg-type]


def test_the_publication_request_carries_both_identities(tmp_path: Path) -> None:
    from touchstone.nodes import publish as publish_node

    context = SimpleNamespace(
        config=SimpleNamespace(
            forge=SimpleNamespace(
                default_branch="main",
                escalation_label="x",
                slug="acme/widgets",
                required_workflows=("ci.yml",),
            ),
            git=_git(),
        ),
        loop=lambda _name: SimpleNamespace(name="code", label="touchstone:audit", auto_merge=False),
    )
    state = {
        "loop": "code",
        "branch": "touchstone/a-b",
        "worktree": str(tmp_path),
        "finding": {"title": "T", "commit_subject": "fix: t"},
    }

    request = publish_node._request(state, context)  # type: ignore[arg-type]

    assert (request.author_name, request.author_email) == BOT
    assert (request.coauthor_name, request.coauthor_email) == OPERATOR
