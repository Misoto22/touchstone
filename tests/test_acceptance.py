"""What the shell implementation learned, kept as executable claims.

Every case here is a bug that reached production, or a near miss found by
running the thing unattended. They are written as behaviour rather than as unit
tests of a function, because each one was originally a failure nobody could see
— an exit code with an empty log, a session that hung only when stdin was a
pipe, a check that passed because a query returned nothing.

If this rewrite is to replace the shell version, it has to keep every one.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone import ledger, visualise
from touchstone.config import ConfigError, SshConfig, load
from touchstone.engines.claude import ClaudeEngine, _payload
from touchstone.engines.codex import CodexEngine
from touchstone.execution.base import Result
from touchstone.execution.ssh import SshExecutor
from touchstone.graph import _after_classify, _after_review
from touchstone.nodes import review


class _Spy:
    """Records the argv an executor would have run."""

    where = "local"
    replaces_environment = True

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
    book.record(status="rehearsed", risk="low", title="one that was only rehearsed")

    handled = " ".join(book.handled_titles())
    assert "a merged one" in handled
    assert "a parked one" in handled
    assert "one the gate abandoned" not in handled
    assert "one that was undone" not in handled
    assert "one that was only rehearsed" not in handled


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


@pytest.mark.parametrize(
    ("engine_type", "name", "credential"),
    (
        (CodexEngine, "codex", "OPENAI_API_KEY"),
        (ClaudeEngine, "claude", "ANTHROPIC_API_KEY"),
    ),
)
def test_model_process_receives_only_its_runtime_and_model_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine_type,
    name: str,
    credential: str,
) -> None:  # type: ignore[no-untyped-def]
    class EnvironmentSpy(_Spy):
        def __init__(self) -> None:
            super().__init__()
            self.environments: list[dict[str, str]] = []

        def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.sent.append(argv)
            self.environments.append(kwargs.get("env") or {})
            return Result(0, '{"result":"ok"}', "")

        def write_text(self, _path: str, _text: str) -> None:
            return None

        def read_text(self, _path: str) -> str:
            return '{"result":"ok"}'

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.setenv(credential, "model-secret")
    monkeypatch.setenv("TOUCHSTONE_STATE_KEY", "state-secret")
    monkeypatch.setenv("GH_TOKEN", "publish-secret")
    spy = EnvironmentSpy()
    config = SimpleNamespace(
        engine=SimpleNamespace(
            name=name,
            model="model",
            audit_effort="medium",
            review_effort="medium",
            timeout_seconds=30,
            extra_args=(),
            budget=SimpleNamespace(audit=1.0, review=1.0),
            sandbox="workspace-write",
        ),
        state_dir=str(tmp_path / "state"),
    )
    engine = engine_type(config, spy)

    engine.author("brief", worktree=str(tmp_path), denied=())
    engine.review("review", worktree=str(tmp_path), schema={"type": "object"})

    model_environments = [
        environment
        for argv, environment in zip(spy.sent, spy.environments, strict=True)
        if argv[0] == name
    ]
    assert len(model_environments) == 2
    for model_environment in model_environments:
        assert model_environment[credential] == "model-secret"
        assert model_environment["HOME"].startswith(str(tmp_path))
        assert "TOUCHSTONE_STATE_KEY" not in model_environment
        assert "GH_TOKEN" not in model_environment


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
    line, and every finding until then had been medium.

    The bound is now a refusal rather than a slice, which answers the same
    concern more completely: nothing is piped, and nothing is reviewed in
    part."""

    from touchstone.execution import Result
    from touchstone.nodes.review import _reviewable_diff

    class _Executor:
        def run(self, argv, timeout=None):  # type: ignore[no-untyped-def]
            if "--intent-to-add" in argv:
                return Result(code=0, stdout="", stderr="")
            return Result(code=0, stdout="x" * (review.DIFF_LIMIT + 1), stderr="")

    class _Context:
        executor = _Executor()

    reviewable = _reviewable_diff(_Context(), "/nowhere", "base-ref")
    assert not reviewable.diff, "an oversized diff was handed to the reviewer anyway"
    assert str(review.DIFF_LIMIT) in reviewable.refusal
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
        'version = 1\n[project]\npath = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[execution]\ntarget = "ssh"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    with pytest.raises(ConfigError, match=r"no \[execution.ssh\]"):
        load(path)


def test_a_configuration_with_no_loops_is_refused(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'version = 1\n[project]\npath = "/tmp/r"\n[forge]\nslug = "o/r"\n',
    )
    with pytest.raises(ConfigError, match="nothing to run"):
        load(path)


def test_an_unknown_engine_is_refused(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'version = 1\n[project]\npath = "/tmp/r"\n'
        '[forge]\nslug = "o/r"\n[engine]\nname = "gemini"\n'
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
        'version = 1\n[project]\npath = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[engine]\nname = "codex"\nmodel = "gpt-5.6-terra"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    monkeypatch.setenv("TOUCHSTONE_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("TOUCHSTONE_EFFORT", "xhigh")
    loaded = load(path)
    assert loaded.engine.model == "gpt-5.6-sol"
    assert loaded.engine.audit_effort == "xhigh"
    assert loaded.forge.slug == "o/r"


def test_the_description_names_the_model_before_anything_is_spent(tmp_path: Path) -> None:
    """A loop that merges to production unattended should never leave anyone
    guessing which model just approved something."""
    path = _config(
        tmp_path,
        'version = 1\n[project]\npath = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[engine]\nname = "codex"\nmodel = "gpt-5.6-sol"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    described = load(path).describe()
    assert "gpt-5.6-sol" in described
    assert "codex" in described
    assert "local" in described


# --- the shipped configuration and briefs agree with each other -------------


def test_the_shipped_example_still_has_its_protected_paths() -> None:
    """A TOML sub-table swallows every key after it until the next header.

    Writing `[loop.code.context]` above `protected_paths` moved the protected
    paths into the context — leaving a loop whose safety check was switched
    off, in a file that looked entirely reasonable. Nothing failed; the check
    simply had nothing to check.
    """
    import tomllib

    root = Path(__file__).resolve().parents[1]
    example = root / "touchstone.example.toml"
    raw = tomllib.loads(example.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert (root / raw["generated"]).is_file()
    for name, table in raw["loop"].items():
        assert table.get("protected_paths"), f"[loop.{name}] has no protected paths"
        assert "protected_paths" not in table.get("context", {}), (
            f"[loop.{name}.context] swallowed protected_paths; move the context table last"
        )


def test_every_placeholder_a_brief_uses_is_supplied() -> None:
    """A brief names what it needs; the configuration says what this project
    calls it. An unnamed placeholder survives as `$word` in the prompt, which is
    a session being asked to audit something whose name is literally a dollar
    sign."""
    import re
    import tomllib

    root = Path(__file__).resolve().parents[1]
    example = root / "touchstone.example.toml"
    raw = tomllib.loads(example.read_text(encoding="utf-8"))
    loaded = load(example)
    brief_root = root / "src" / "touchstone" / "resources" / "briefs"
    shared = re.findall(r"\$(\w+)", (brief_root / "review.md").read_text(encoding="utf-8"))

    for name, table in raw["loop"].items():
        reference = table["brief"]
        brief = (
            brief_root / f"{reference.removeprefix('builtin:')}.md"
            if reference.startswith("builtin:")
            else root / reference
        )
        assert brief.exists(), f"[loop.{name}] points at a brief that is not there: {brief}"
        used = set(re.findall(r"\$(\w+)", brief.read_text(encoding="utf-8"))) | set(shared)
        missing = used - set(dict(loaded.loop(name).context))
        assert not missing, f"[loop.{name}.context] is missing {sorted(missing)}"


def test_the_briefs_keep_the_constraints_that_were_paid_for() -> None:
    """Each of these sentences is in a brief because its absence cost something.

    A rewrite is free to reword them. It is not free to drop them, and a test
    that names them makes deleting one a deliberate act rather than a tidy-up.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "touchstone" / "resources" / "briefs"
    audit = (root / "code-audit.md").read_text(encoding="utf-8")
    harness = (root / "harness-review.md").read_text(encoding="utf-8")
    review = (root / "review.md").read_text(encoding="utf-8")

    # A finding reported as an outage that had never happened.
    assert "latent" in audit and "past tense" in audit
    # A translated document left on its seed version, drifting daily.
    assert "twin" in audit and "twin" in harness
    # The register became a work queue, and these three sentences are the whole
    # reason that is safe: one violation at a time so the diff can be read, a
    # count that can only fall, and a new enforcer recorded at what it actually
    # finds rather than at zero.
    # Collapsed, because these are sentences and a sentence wraps wherever the
    # line ends. An assertion that breaks on a reflow tests the margin, not the
    # constraint.
    flowed = " ".join(audit.split())
    assert "Remove **one** violation" in flowed
    assert "Never raise one" in flowed
    assert "Record what it found" in flowed
    # kioku protects its register from the loop that would edit it. Without
    # this the register queue makes every run touch a protected path, which
    # forces the highest risk class and parks the change — quietly, each time.
    assert "yours to write" in flowed
    # A ceiling raised instead of a regression reported.
    assert "never raised" in harness
    # A date stamped in UTC by a schedule that runs on local time.
    assert "not UTC" in harness
    # A reviewer that agreed by default is not a reviewer.
    assert "owe it nothing" in review
    # Understating risk to get something merged.
    assert "Understating risk" in audit and "Understating risk" in harness


def test_the_description_names_where_it_runs_not_what_is_configured(tmp_path: Path) -> None:
    """An `[execution.ssh]` section can exist while the target is local.

    Reading the section rather than the target reported `ssh my-server` for a
    run happening on this machine — a loop that merges to production
    misreporting which host it is on is exactly what this line exists to stop.
    """
    path = _config(
        tmp_path,
        'version = 1\n[project]\npath = "/tmp/r"\n[forge]\nslug = "o/r"\n'
        '[execution]\ntarget = "local"\n'
        '[execution.ssh]\nhost = "elsewhere"\nworkdir = "/w"\nstate_dir = "/s"\n'
        '[loop.code]\nbrief = "b.md"\nlabel = "l"\n',
    )
    described = load(path).describe()
    assert "local" in described
    assert "elsewhere" not in described


def test_a_rehearsal_is_stopped_only_by_the_kill_switch(tmp_path: Path) -> None:
    """A dry run publishes nothing, so the gates about publishing do not apply.

    Checked in the other order once, and the effect was that a rehearsal could
    not run while any pull request was open — which, for a loop whose job is
    opening pull requests, is nearly always. `PAUSED` still holds, because that
    one means a person said stop rather than describing a condition.
    """
    import inspect

    from touchstone import runner

    source = inspect.getsource(runner._gates)
    paused_at = source.index("PAUSED")
    dry_at = source.index("if dry_run")
    slot_at = source.index("open_pulls")
    health_at = source.index("_health_gate(config)")
    assert paused_at < dry_at < slot_at, "the slot gate runs before the dry-run exit"
    assert dry_at < health_at, "the health gate runs before the dry-run exit"


def test_a_rehearsal_never_reaches_the_forge() -> None:
    """`--dry-run` has to be visible to the nodes that publish.

    Passed only to the gates once, and the effect was a rehearsal that opened a
    real pull request while printing `clean`. A rehearsal that publishes is
    worse than no rehearsal, because it is trusted not to.
    """
    import inspect

    from touchstone import graph

    source = inspect.getsource(graph)
    assert "dry_run: bool" in source, "dry_run does not travel in the graph state"
    assert source.count('state.get("dry_run")') >= 2, (
        "both publishing nodes must check for a rehearsal"
    )


def test_the_runner_asks_the_graph_whether_it_paused() -> None:
    """An interrupt returns the state as it stood before the node returned.

    Reading `outcome` from that payload gets the default, which is how one run
    reported `clean` while its pull request sat open.
    """
    import inspect

    from touchstone import runner

    source = inspect.getsource(runner.execute)
    assert "get_state(thread).next" in source

    # Change lifecycle rows belong only to actual candidates. Gate holds and
    # rehearsals are Run Outcomes in events.jsonl, not fake candidate states.
    records = [line for line in source.splitlines() if "ledger.record" in line]
    assert records == []
    assert "RunOutcome.BLOCKED" in source


def test_the_checks_see_everything_the_commit_will_carry() -> None:
    """`git add -A` sweeps up untracked files; `git diff --name-only` does not.

    Checking with the narrower view let a run confined to one directory publish
    three of its own scratch files from the repository root, with neither the
    confinement nor the protected paths noticing. A check that sees less than
    the action it guards is not a guard.
    """
    import inspect

    from touchstone.nodes import classify

    source = inspect.getsource(classify._changed)
    assert "--untracked-files=all" in source
    assert "diff" not in source.split('"""')[-1], "still diffing rather than reading status"


def test_the_loop_removes_its_own_scratch_files() -> None:
    """Every file the loop writes into the worktree to talk to itself is read
    once and deleted, so `git add -A` cannot find it."""
    import inspect

    from touchstone.engines import claude, codex
    from touchstone.nodes import audit

    assert "rm" in inspect.getsource(audit.run), "the finding file survives the run"
    assert "rm" in inspect.getsource(codex.CodexEngine.review), "the schema and answer survive"
    assert "rm" in inspect.getsource(claude.ClaudeEngine.author), "the settings file survives"


def test_answering_a_parked_thread_does_not_open_a_second_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node that interrupts re-executes from its first line when resumed.

    Publishing and waiting in one node therefore opened a draft, stopped, and
    opened another draft the moment a person answered. Splitting them puts a
    checkpoint between the side effect and the wait, which is what makes the
    side effect happen once — and only a real resume shows it, because the
    first half looks perfect on its own.

    `monkeypatch` rather than plain assignment: replacing a module's functions
    outright leaves them replaced for every test that runs afterwards, which is
    how the ledger test below passed on its own and failed in the suite.
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from touchstone import graph as G

    for answer, expected in (("approve", "awaiting_checks"), ("close", "closed")):
        calls: list[str] = []
        monkeypatch.setattr(
            G.publish, "park", lambda s, c=calls: (c.append("park"), {"pr": 999})[1]
        )
        monkeypatch.setattr(
            G.publish,
            "arm_merge",
            lambda s, c=calls: (c.append("approve"), {"outcome": "awaiting_checks"})[1],
        )
        monkeypatch.setattr(
            G.publish,
            "record_closed",
            lambda s, c=calls: (c.append("close"), {"outcome": "closed"})[1],
        )
        monkeypatch.setattr(
            G.audit,
            "run",
            lambda s: {"finding": {"status": "proposed", "risk": "medium", "title": "t"}},
        )
        monkeypatch.setattr(G.classify, "run", lambda s: {"risk": "medium"})

        app = G.build().compile(checkpointer=InMemorySaver())
        thread = {"configurable": {"thread_id": f"t-{answer}"}}
        app.invoke({"loop": "h", "worktree": "/w", "branch": "b", "dry_run": False}, thread)

        assert app.get_state(thread).next, f"{answer}: the thread did not pause"
        final = app.invoke(Command(resume=answer), thread)

        assert calls.count("park") == 1, f"{answer}: published {calls.count('park')} times"
        assert final.get("outcome") == expected


def test_every_ending_leaves_a_row_in_the_ledger() -> None:
    """A run that found nothing still has to say so.

    The point of a scheduled loop is that its absence is noticeable: silence
    means the review did not run, and that is itself a finding. A `clean`
    outcome writing no row makes "ran and found nothing" indistinguishable from
    "never started" — the one distinction the ledger exists to preserve. It was
    silent for exactly that outcome until a real run on the server ended
    `clean` and left nothing behind.
    """
    import inspect

    from touchstone import runner
    from touchstone.nodes import audit, classify, publish

    assert "ledger.record" in inspect.getsource(audit._clean)

    # Nothing may end a run clean without going through `_clean`.
    helper = inspect.getsource(audit._clean)
    for module in (audit, classify):
        body = inspect.getsource(module).replace(helper, "")
        assert '"outcome": "clean"' not in body, (
            f"{module.__name__} ends a run clean without recording it"
        )

    # Publishing routes through the lifecycle service, which appends one event
    # for each candidate transition. Rehearsals are Run Outcomes only, while
    # the runner records every terminal outcome.
    assert "RepositoryLifecycle" in inspect.getsource(publish._publish)
    assert "RepositoryLifecycle" in inspect.getsource(publish._resume)
    assert "ledger.record" not in inspect.getsource(publish.rehearse)
    assert 'kind="finished"' in inspect.getsource(runner.execute)


def test_the_reviewer_sees_the_files_the_commit_will_create(tmp_path) -> None:
    """`git diff` omits untracked files; `git add -A` commits them.

    Two pull requests were rejected for "including neither the implementation
    nor the claimed test" while both were in the diff — as new files, invisible
    to the reviewer. The wrong reject is the cheap direction. The same gap
    approves a change whose entire risk is in a file the reviewer never read.
    """
    import subprocess

    from touchstone.execution import LocalExecutor
    from touchstone.nodes.review import _reviewable_diff

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*argv: str) -> None:
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "existing.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("branch", "-q", "base-ref")

    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "brand_new.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")

    class _Context:
        executor = LocalExecutor()

    reviewable = _reviewable_diff(_Context(), str(repo), "base-ref")

    assert not reviewable.refusal
    diff = reviewable.diff
    assert "existing.py" in diff, "the tracked change is missing"
    assert "brand_new.py" in diff, "a file the commit will create is missing from the review"
    assert "def helper() -> int:" in diff, "the new file's contents never reach the reviewer"


def test_an_incomplete_diff_is_refused_rather_than_narrowed(monkeypatch, tmp_path) -> None:
    """Falling back to the tracked-only diff would review a subset and return a
    verdict on the whole, which is the failure the staging step removes."""
    from touchstone.execution import Result
    from touchstone.nodes.review import _reviewable_diff

    class _Executor:
        def run(self, argv, timeout=None):  # type: ignore[no-untyped-def]
            if "--intent-to-add" in argv:
                return Result(code=1, stdout="", stderr="index.lock exists")
            raise AssertionError("the diff was read after staging failed")

    class _Context:
        executor = _Executor()

    reviewable = _reviewable_diff(_Context(), str(tmp_path), "base-ref")
    assert not reviewable.diff
    assert reviewable.refusal


def test_a_large_new_file_cannot_push_a_tracked_edit_out_of_the_review(tmp_path) -> None:
    """The exact case the size bound had to become a refusal to answer.

    Git emits patches in path order. A generated `a_generated.txt` therefore
    sorts ahead of `z.py`, and while the bound was a slice the reviewer got the
    generated file and returned a verdict on a change that also edited `z.py`.
    Before new files were included at all the tracked edit was always visible,
    so including them without this would have traded one blindness for a worse
    one.
    """
    import subprocess

    from touchstone.execution import LocalExecutor
    from touchstone.nodes.review import DIFF_LIMIT, _reviewable_diff

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*argv: str) -> None:
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "z.py").write_text("SECRET = read_from_env()\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("branch", "-q", "base-ref")

    (repo / "z.py").write_text("SECRET = 'hardcoded'\n", encoding="utf-8")
    (repo / "a_generated.txt").write_text("x\n" * DIFF_LIMIT, encoding="utf-8")

    class _Context:
        executor = LocalExecutor()

    reviewable = _reviewable_diff(_Context(), str(repo), "base-ref")

    assert reviewable.refusal, "an oversized change was reviewed on whatever fit"
    assert not reviewable.diff


def test_an_ssh_engine_keeps_the_remote_environment_it_was_configured_with() -> None:
    """An ssh command cannot have its environment replaced, only added to.

    Prepending a locally derived environment would override the configured
    remote PATH and HOME and put a local API key on a remote command line.
    """

    class _RemoteSpy(_Spy):
        where = "ssh my-server"
        replaces_environment = False

        def __init__(self) -> None:
            super().__init__()
            self.environments: list[dict[str, str] | None] = []

        def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.sent.append(argv)
            self.environments.append(kwargs.get("env"))
            return Result(0, '{"result":"ok"}', "")

        def read_text(self, _path: str) -> str:
            return '{"result":"ok"}'

        def write_text(self, _path: str, _text: str) -> None:
            return None

    spy = _RemoteSpy()
    config = SimpleNamespace(
        engine=SimpleNamespace(
            name="codex",
            model="model",
            audit_effort="medium",
            review_effort="medium",
            timeout_seconds=30,
            extra_args=(),
            budget=SimpleNamespace(audit=1.0, review=1.0),
            sandbox="workspace-write",
        ),
        state_dir="/tmp/touchstone-remote-test",
    )

    CodexEngine(config, spy).author("brief", worktree="/remote/worktree", denied=())

    model_calls = [
        environment
        for argv, environment in zip(spy.sent, spy.environments, strict=True)
        if argv[0] == "codex"
    ]
    assert model_calls == [None]
