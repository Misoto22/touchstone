"""The sections a brief is told were collected before the session ran."""

from __future__ import annotations

from types import SimpleNamespace

from touchstone.execution.base import Result
from touchstone.nodes.audit import EVIDENCE_LIMIT, _evidence


class _Executor:
    def __init__(self, *results: Result) -> None:
        self._results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, argv, *, cwd=None, timeout=None):  # type: ignore[no-untyped-def]
        self.calls.append((argv, cwd))
        return self._results.pop(0)


def _loop(*evidence):  # type: ignore[no-untyped-def]
    return SimpleNamespace(evidence=tuple(evidence))


def test_a_loop_without_evidence_adds_nothing() -> None:
    executor = _Executor()
    assert _evidence(SimpleNamespace(executor=executor), _loop(), "/tree") == ""
    assert executor.calls == []


def test_collected_output_reaches_the_prompt_under_its_heading() -> None:
    executor = _Executor(Result(code=0, stdout="rows: 54\n", stderr=""))
    loop = _loop(("The census", ("just", "census")))

    text = _evidence(SimpleNamespace(executor=executor), loop, "/tree")

    assert "### The census" in text
    assert "rows: 54" in text
    assert executor.calls == [(["just", "census"], "/tree")], "not run in the run's own worktree"


def test_a_failed_command_says_so_rather_than_going_missing() -> None:
    """An absent heading reads as evidence nobody asked for. The brief acts on
    the difference: "if a section says it is unavailable, that is the real state
    of the world"."""
    executor = _Executor(Result(code=127, stdout="", stderr="just: not found"))
    loop = _loop(("The census", ("just", "census")))

    text = _evidence(SimpleNamespace(executor=executor), loop, "/tree")

    assert "### The census" in text
    assert "unavailable" in text
    assert "127" in text


def test_oversized_evidence_is_refused_rather_than_cut() -> None:
    """Half a census cannot say whether a ratchet regressed, while looking like
    it can — the same reason the review refuses an oversized diff."""
    executor = _Executor(Result(code=0, stdout="x" * (EVIDENCE_LIMIT + 1), stderr=""))
    loop = _loop(("The census", ("just", "census")))

    text = _evidence(SimpleNamespace(executor=executor), loop, "/tree")

    assert "unavailable" in text
    assert "x" * 200 not in text, "the oversized output was pasted in anyway"


def test_sections_keep_the_order_they_were_configured_in() -> None:
    executor = _Executor(
        Result(code=0, stdout="first", stderr=""),
        Result(code=0, stdout="second", stderr=""),
    )
    loop = _loop(("The census", ("a",)), ("The latest CI run", ("b",)))

    text = _evidence(SimpleNamespace(executor=executor), loop, "/tree")

    assert text.index("The census") < text.index("The latest CI run")
