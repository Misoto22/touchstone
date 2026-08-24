"""What the shell implementation learned, kept as executable claims.

Every case here is a bug that reached production, or a near miss found by
running the thing unattended. They are written as behaviour rather than as unit
tests of a function, because each one was originally a failure nobody could see
— an exit code with an empty log, a session that hung only when stdin was a
pipe, a check that passed because a query returned nothing.

If this rewrite is to replace the shell version, it has to keep every one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_loop import ledger, visualise
from harness_loop.config import ConfigError, SshConfig, load
from harness_loop.engines.claude import ClaudeEngine, _payload
from harness_loop.engines.codex import CodexEngine
from harness_loop.execution.base import Result
from harness_loop.execution.ssh import SshExecutor
from harness_loop.graph import _after_classify, _after_review
from harness_loop.nodes import review


class _Spy:
    """Records the argv an executor would have run."""

    where = "local"

    def __init__(self) -> None:
        self.sent: list[list[str]] = []

    def run(self, argv, **_):  # type: ignore[no-untyped-def]
        self.sent.append(argv)
        return Result(0, "", "")


def _ssh_with(spy: _Spy) -> SshExecutor:
    executor = SshExecutor(SshConfig(host="h", workdir="/w", state_dir="/s"))
    executor._local = spy  # type: ignore[assignment]
    return executor


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "c.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the graph is the shape it claims to be ---------------------------------


def test_only_a_low_risk_finding_reaches_the_review() -> None:
    """Anything higher goes to a person whatever a reviewer would say.

    A second session to be told so costs a seventh of an audit for an answer
    that changes nothing.
    """
    assert _after_classify({"risk": "low"}) == "review"
    assert _after_classify({"risk": "medium"}) == "park"
    assert _after_classify({"risk": "high"}) == "park"


def test_a_rejected_review_parks_rather_than_merges() -> None:
    assert _after_review({"verdict": "approve"}) == "merge"
    assert _after_review({"verdict": "reject"}) == "park"
    assert _after_review({}) == "park"


def test_the_committed_diagram_matches_the_graph() -> None:
    """A diagram pasted into a README is true once. This one is checked."""
    ok, message = visualise.check(Path(__file__).resolve().parents[1])
    assert ok, message


# --- the ledger is the loop's memory, and it was wrong once -----------------


def test_only_a_finding_with_somewhere_to_live_counts_as_handled(tmp_path: Path) -> None:
    """`held` is not handled.

    Written first as a denylist of one status, which let an abandoned finding
    into the "already handled" list and hid a defect nobody fixed. An allowlist
    is one new status away from being right; a denylist is one away from being
    wrong again.
    """
    book = ledger.Ledger(tmp_path / "ledger.jsonl")
    book.record(status="merging", risk="low", title="a merged one")
    book.record(status="escalated", risk="medium", title="a parked one")
    book.record(status="held", risk="low", title="one the gate abandoned")
    book.record(status="reverted", risk="low", title="one that was undone")

    handled = " ".join(book.handled_titles())
    assert "a merged one" in handled
    assert "a parked one" in handled
    assert "one the gate abandoned" not in handled
    assert "one that was undone" not in handled


def test_a_truncated_ledger_line_loses_a_record_not_the_run(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    book = ledger.Ledger(path)
    book.record(status="merging", title="intact")
    path.write_text(path.read_text(encoding="utf-8") + '{"status": "merging", "titl\n')
    assert [row["title"] for row in book.rows()] == ["intact"]


# --- the engines differ, and the difference has teeth -----------------------


def test_each_engine_declares_what_it_cannot_do() -> None:
    """Stated in the type rather than discovered from a blank column.

    Codex reports no cost, so spend is invisible and the clock is the only
    ceiling; and it takes a sandbox rather than a deny list, which leaves the
    diff check as the only path enforcement there is.
    """
    assert CodexEngine.reports_cost is False
    assert CodexEngine.enforces_paths is False
    assert ClaudeEngine.reports_cost is True
    assert ClaudeEngine.enforces_paths is True


def test_a_malformed_envelope_yields_no_cost_rather_than_a_wrong_one() -> None:
    assert _payload("not json") == (None, "")
    assert _payload('{"total_cost_usd": 7.62, "result": "ok"}') == (7.62, "ok")
    assert _payload('{"result": "ok"}') == (None, "ok")


# --- the failures that only appeared when nobody was watching ---------------


def test_a_remote_command_closes_stdin() -> None:
    """Codex appends piped stdin to its prompt.

    With stdin left open it blocks on a read that never returns — invisibly,
    because from a terminal stdin is a tty and reaches EOF. Fifty minutes and
    one silent hang before anyone noticed.
    """
    spy = _Spy()
    _ssh_with(spy).run(["codex", "exec"], timeout=10)
    assert spy.sent[0][-1].endswith("</dev/null")


def test_a_remote_command_is_quoted_not_interpolated() -> None:
    """A brief is one of these arguments: kilobytes of prose carrying
    backticks, quotes and dollar signs, bound for a remote shell. String
    interpolation there is not a style question."""
    spy = _Spy()
    _ssh_with(spy).run(["echo", "$(id); `whoami`"], timeout=10)
    remote = spy.sent[0][-1]
    assert "'$(id); `whoami`'" in remote


def test_a_remote_command_is_bounded_on_the_far_side_too() -> None:
    """A local timeout leaves the remote side running, and the next run finds a
    lock whose pid is alive on a machine nobody is watching."""
    spy = _Spy()
    _ssh_with(spy).run(["sleep", "1"], timeout=30)
    assert "timeout 30" in spy.sent[0][-1]


def test_the_diff_is_truncated_without_a_pipe() -> None:
    """`| head -c` closes the pipe, git takes SIGPIPE, and under `pipefail` the
    whole run dies — silently, twenty-two minutes and one correct finding in.
    It survived every earlier test because only a low-risk finding reaches this
    line, and every finding until then had been medium."""
    import ast

    tree = ast.parse(Path(review.__file__).read_text(encoding="utf-8"))
    # The truncation is a slice in Python, not an argument to another process.
    # Asserting on the source text would only prove the comment explaining this
    # still mentions `head -c`, which is how this test failed when it was first
    # written — testing the prose rather than the behaviour.
    slices = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
    ]
    assert slices, "the diff is not sliced anywhere"
    assert any(
        isinstance(node.slice.upper, ast.Name) and node.slice.upper.id == "DIFF_LIMIT"
        for node in slices
    ), "the diff is not bounded by DIFF_LIMIT"
    assert review.DIFF_LIMIT == 60_000


def test_a_verdict_is_a_validated_field_not_the_last_word_in_prose() -> None:
    """Grepping free text for the final `approve` or `reject` is correct for
    most phrasings and guaranteed by none of them — immediately in front of an
    unattended production merge."""
    assert review.SCHEMA["properties"]["verdict"]["enum"] == ["approve", "reject"]
    assert review.SCHEMA["required"] == ["verdict", "reason"]
    assert review.SCHEMA["additionalProperties"] is False


# --- configuration refuses rather than guesses ------------------------------


def test_ssh_without_a_host_section_is_refused(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'repo_path = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[execution]\ntarget = "ssh"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    with pytest.raises(ConfigError, match=r"no \[execution.ssh\]"):
        load(path)


def test_a_configuration_with_no_loops_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, 'repo_path = "/tmp/r"\n[forge]\nslug = "o/r"\n')
    with pytest.raises(ConfigError, match="nothing to run"):
        load(path)


def test_an_unknown_engine_is_refused(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'repo_path = "/tmp/r"\n[forge]\nslug = "o/r"\n[engine]\nname = "gemini"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    with pytest.raises(ConfigError, match=r"codex.*claude"):
        load(path)


def test_the_environment_overrides_only_the_volatile_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine, model, effort and where it runs are what an operator changes
    while debugging. Everything structural stays in the file, reviewable."""
    path = _config(
        tmp_path,
        'repo_path = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[engine]\nname = "codex"\nmodel = "gpt-5.6-terra"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    monkeypatch.setenv("HARNESS_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("HARNESS_EFFORT", "xhigh")
    loaded = load(path)
    assert loaded.engine.model == "gpt-5.6-sol"
    assert loaded.engine.audit_effort == "xhigh"
    assert loaded.forge.slug == "o/r"


def test_the_description_names_the_model_before_anything_is_spent(tmp_path: Path) -> None:
    """A loop that merges to production unattended should never leave anyone
    guessing which model just approved something."""
    path = _config(
        tmp_path,
        'repo_path = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[engine]\nname = "codex"\nmodel = "gpt-5.6-sol"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    described = load(path).describe()
    assert "gpt-5.6-sol" in described
    assert "codex" in described
    assert "local" in described
