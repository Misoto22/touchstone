from __future__ import annotations

from types import SimpleNamespace

from touchstone.execution.base import Result
from touchstone.nodes import audit, review


class _Executor:
    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            return Result(0, "diff --git a/a b/a\n", "")
        return Result(0, "", "")


def test_audit_author_receives_the_resolved_harness_header(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    prompts: list[str] = []
    loop = SimpleNamespace(
        name="code",
        prompt=lambda: "AUDIT BRIEF",
        protected_paths=(),
        model="",
        attachment=(),
    )
    engine = SimpleNamespace(
        name="codex",
        author=lambda prompt, **_kwargs: (
            prompts.append(prompt) or SimpleNamespace(blocked="", ok=False, cost=0.0)
        ),
    )
    context = SimpleNamespace(
        loop=lambda _name: loop,
        harness_prompt=lambda: "RESOLVED HARNESS\n\n",
        ledger=SimpleNamespace(handled_titles=lambda: ()),
        engine_for=lambda _name: engine,
    )
    monkeypatch.setattr(audit, "current", lambda: context)

    audit.run({"loop": "code", "worktree": "/worktree"})

    assert prompts == ["RESOLVED HARNESS\n\nAUDIT BRIEF"]


def test_reviewer_receives_the_same_resolved_harness_header(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    prompts: list[str] = []
    loop = SimpleNamespace(
        name="code",
        review_prompt=lambda: "REVIEW BRIEF",
        context=(),
        model="",
    )
    engine = SimpleNamespace(
        name="codex",
        review=lambda prompt, **_kwargs: (
            prompts.append(prompt) or SimpleNamespace(ok=False, cost=0.0)
        ),
    )
    context = SimpleNamespace(
        loop=lambda _name: loop,
        harness_prompt=lambda: "RESOLVED HARNESS\n\n",
        executor=_Executor(),
        engine_for=lambda _name: engine,
        config=SimpleNamespace(forge=SimpleNamespace(default_branch="main")),
    )
    monkeypatch.setattr(review, "current", lambda: context)

    review.run(
        {
            "loop": "code",
            "worktree": "/worktree",
            "finding": {"title": "Finding", "summary": "Summary"},
        }
    )

    assert prompts[0].startswith("RESOLVED HARNESS\n\nREVIEW BRIEF")
