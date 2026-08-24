"""The sections a brief is told were collected before the session ran."""

from __future__ import annotations

from types import SimpleNamespace

from touchstone.execution.base import Result
from touchstone.nodes.audit import EVIDENCE_LIMIT, _evidence


class _Executor:
    def __init__(self, *results: Result) -> None:
        self._results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, argv, *, cwd=None, timeout=None, env=None):  # type: ignore[no-untyped-def]
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


def test_evidence_runs_without_the_credentials_around_it(monkeypatch) -> None:
    """A hosted Analysis step holds the model key and `TOUCHSTONE_STATE_KEY`,
    and an evidence command is repository-authored — `just census` is whatever
    the justfile and its dependencies say today. The rest of the system already
    scrubs the environment before every model, preparation and validation
    subprocess; this must not be the way around that."""
    for name, value in (
        ("OPENAI_API_KEY", "sk-secret"),
        ("ANTHROPIC_API_KEY", "sk-ant-secret"),
        ("TOUCHSTONE_STATE_KEY", "state-secret"),
        ("GITHUB_TOKEN", "ghs-secret"),
        ("PATH", "/usr/bin"),
    ):
        monkeypatch.setenv(name, value)

    seen: dict[str, str] = {}

    class _Recording:
        def run(self, argv, *, cwd=None, timeout=None, env=None):  # type: ignore[no-untyped-def]
            seen.update(env or {})
            return Result(code=0, stdout="ok", stderr="")

    _evidence(
        SimpleNamespace(executor=_Recording()),
        _loop(("The census", ("just", "census"))),
        "/tree",
    )

    assert "PATH" in seen, "a scrubbed environment still has to be able to run anything"
    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TOUCHSTONE_STATE_KEY", "GITHUB_TOKEN"):
        assert secret not in seen, f"{secret} reached a repository-authored command"
    assert "secret" not in "".join(seen.values())


def test_a_command_that_cannot_start_does_not_end_the_run() -> None:
    """`subprocess.run` raises rather than returning a code when the executable
    is absent. Uncaught, a host without `just` aborts the whole audit instead of
    producing the headed `unavailable` section this promises — and the entries
    after it are never collected either."""

    class _Missing:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, argv, *, cwd=None, timeout=None, env=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            if argv[0] == "just":
                raise FileNotFoundError(2, "No such file or directory")
            return Result(code=0, stdout="later output", stderr="")

    executor = _Missing()
    text = _evidence(
        SimpleNamespace(executor=executor),
        _loop(("The census", ("just", "census")), ("The latest CI run", ("gh", "run"))),
        "/tree",
    )

    assert "### The census" in text
    assert "could not start" in text
    assert "later output" in text, "collection stopped at the first missing executable"
    assert executor.calls == 2


def test_changing_evidence_changes_the_configuration_digest() -> None:
    """Evidence lands in the audit prompt, so it is an Analysis input. Left out
    of the digest, a changed command keeps naming the same state artifact and a
    snapshot taken under the old evidence is restored as compatible."""
    from touchstone.hosted.snapshot import _loop_config

    base = SimpleNamespace(
        name="code",
        brief="builtin:code-audit",
        label="touchstone:audit",
        evidence=(("The census", ("just", "census")),),
    )
    changed_command = SimpleNamespace(
        **{**vars(base), "evidence": (("The census", ("just", "c")),)}
    )
    changed_heading = SimpleNamespace(
        **{**vars(base), "evidence": (("The ratchet census", ("just", "census")),)}
    )

    assert _loop_config(base) != _loop_config(changed_command)
    assert _loop_config(base) != _loop_config(changed_heading)
