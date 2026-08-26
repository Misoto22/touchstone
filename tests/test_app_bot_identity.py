from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from touchstone.hosted import runtime
from touchstone.hosted.workflow import ActionPins, render_workflow


class _Response(io.BytesIO):
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
        return False


def _fake_users(monkeypatch, payload, captured):  # type: ignore[no-untyped-def]
    def urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured.append((request.full_url, dict(request.headers)))
        if payload is None:
            raise OSError("users endpoint unavailable")
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)


def test_the_bot_identity_links_the_commit_to_the_publishing_app(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: list[tuple[str, dict]] = []
    _fake_users(monkeypatch, {"id": 4242, "login": "acme-touchstone[bot]"}, captured)

    identity = runtime._app_bot_identity(
        {"TOUCHSTONE_APP_SLUG": "acme-touchstone", "GH_TOKEN": "app-token"}
    )

    assert identity == (
        "acme-touchstone[bot]",
        "4242+acme-touchstone[bot]@users.noreply.github.com",
    )
    url, headers = captured[0]
    assert url == "https://api.github.com/users/acme-touchstone%5Bbot%5D"
    assert headers["Authorization"] == "Bearer app-token"


def test_an_unreachable_account_lookup_still_attributes_the_app(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _fake_users(monkeypatch, None, [])

    identity = runtime._app_bot_identity(
        {"TOUCHSTONE_APP_SLUG": "acme-touchstone", "GH_TOKEN": "app-token"}
    )

    assert identity == (
        "acme-touchstone[bot]",
        "acme-touchstone[bot]@users.noreply.github.com",
    )


@pytest.mark.parametrize(
    "slug",
    ["", "  ", "bad slug", "acme;rm -rf /", "-leading", "a" * 64, "acme/other"],
)
def test_an_unusable_slug_never_reaches_a_git_argument(slug: str) -> None:
    assert runtime._app_bot_identity({"TOUCHSTONE_APP_SLUG": slug, "GH_TOKEN": "t"}) is None


def test_a_local_hosted_stage_without_a_slug_keeps_the_configured_author() -> None:
    assert runtime._app_bot_identity({"GH_TOKEN": "t"}) is None


def test_the_publication_request_prefers_the_supplied_identity(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from touchstone.config import GitConfig
    from touchstone.nodes import publish as publish_node

    context = SimpleNamespace(
        config=SimpleNamespace(
            forge=SimpleNamespace(
                default_branch="main",
                escalation_label="x",
                slug="acme/widgets",
                required_workflows=("ci.yml",),
            ),
            timezone="UTC",
            git=GitConfig(author_name="Project Author", author_email="project@example.test"),
        ),
        loop=lambda _name: SimpleNamespace(
            name="code",
            label="touchstone:audit",
            auto_merge=False,
            auto_merge_strategy="squash",
            auto_merge_delete_branch=True,
            auto_merge_window=(),
            auto_merge_max_files=0,
        ),
    )
    base = {
        "loop": "code",
        "branch": "touchstone/a-b",
        "worktree": str(tmp_path),
        "finding": {"title": "T", "commit_subject": "fix: t"},
    }

    hosted = publish_node._request(
        base | {"author_name": "acme[bot]", "author_email": "1+acme[bot]@users.noreply.github.com"},
        context,
    )
    local = publish_node._request(base, context)

    assert hosted.author_name == "acme[bot]"
    assert hosted.author_email == "1+acme[bot]@users.noreply.github.com"
    assert local.author_name == "Project Author"


def test_the_generated_workflow_hands_publish_the_app_slug(tmp_path: Path) -> None:
    from tests.test_actions_workflow import _config

    workflow = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    publish = workflow.split("  publish:", 1)[1]
    analysis = workflow.split("  analysis:", 1)[1].split("  verify:", 1)[0]

    assert "app-slug: ${{ steps.app-token.outputs.app-slug }}" in publish
    # The slug is publication-only; no other stage receives it.
    assert "app-slug:" not in analysis
    assert (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8").count(
        "TOUCHSTONE_APP_SLUG"
    ) == 1
