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


def test_the_banner_names_the_model_that_will_run(tmp_path) -> None:
    """This line is the only place an operator reading the journal sees which
    model ran. Reporting the global one said `gpt-5.6-sol` on the first run
    after a loop moved to another model — evidence, where anyone would look,
    that the change had not taken effect."""
    from touchstone.config import load_config

    path = tmp_path / "touchstone.toml"
    path.write_text(
        """version = 1

[project]
path = "."

[forge]
slug = "acme/widgets"

[engine]
name = "codex"
model = "gpt-global"

[loop.code]
brief = "builtin:code-audit"
label = "t:audit"

[loop.code.context]
project = "p"
ledger = "l"
protected = "x"
register = "r"
rules_clause = ""

[loop.harness]
brief = "builtin:harness-review"
label = "t:harness"
model = "gpt-5.6-terra"

[loop.harness.context]
spec = "s"
rules = "r"
ledger = "l"
decisions = "d"
writable = "w"
rules_clause = ""
""",
        encoding="utf-8",
    )
    config = load_config(path)

    assert "model=gpt-5.6-terra (loop)" in config.describe("harness")
    assert "model=gpt-global" in config.describe("code")
    # `run-due` wakes several loops; no single model is the one that will run.
    assert "model=gpt-global" in config.describe()
