"""A loop may name a model of its own."""

from __future__ import annotations

from types import SimpleNamespace

from touchstone.engines.codex import CodexEngine


def _config(model: str = "gpt-global"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        engine=SimpleNamespace(
            model=model,
            audit_effort="high",
            review_effort="high",
            sandbox="workspace-write",
            timeout_seconds=2700,
            extra_args=(),
        ),
        state_dir="/tmp",
    )


def _model_on(argv: list[str]) -> str | None:
    return argv[argv.index("-m") + 1] if "-m" in argv else None


def test_a_loop_without_a_model_gets_the_engines() -> None:
    engine = CodexEngine(_config(), SimpleNamespace())
    argv = engine._argv(worktree="/tree", effort="high", sandbox="workspace-write")
    assert _model_on(argv) == "gpt-global"


def test_a_loop_that_names_a_model_gets_that_one() -> None:
    """The loops do different work — implementing a fix against judging a
    harness — and one model need not suit both."""
    engine = CodexEngine(_config(), SimpleNamespace())
    argv = engine._argv(
        worktree="/tree", effort="high", sandbox="workspace-write", model="gpt-5.6-terra"
    )
    assert _model_on(argv) == "gpt-5.6-terra", "the loop's choice never reached the command line"


def test_an_empty_override_does_not_erase_the_engines() -> None:
    """`model=""` is "this loop did not choose", not "run with no model"."""
    engine = CodexEngine(_config(), SimpleNamespace())
    argv = engine._argv(worktree="/tree", effort="high", sandbox="workspace-write", model="")
    assert _model_on(argv) == "gpt-global"


def test_the_model_is_part_of_the_configuration_digest() -> None:
    """A different model is a different Analysis, so a state snapshot taken
    under one is not compatible with a run under another."""
    from touchstone.hosted.snapshot import _loop_config

    base = SimpleNamespace(name="harness", brief="b", label="l", model="")
    named = SimpleNamespace(name="harness", brief="b", label="l", model="gpt-5.6-terra")
    assert _loop_config(base) != _loop_config(named)
