"""The files an unmerged candidate occupies, and how a later session hears about them.

Suppressed titles stop one session raising a defect a previous one already
raised. They said nothing about *where* that previous session wrote, so two runs
an hour apart picked two different defects in one file and the second pull
request to merge arrived conflicted. Both read as clean until then.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from touchstone.execution.base import Result
from touchstone.ledger import Ledger, LifecycleEvent, finding_id
from touchstone.nodes import classify
from touchstone.nodes.audit import _open_changes


def _event(state: str, *, title: str, paths: tuple[str, ...], pr: int | None = 12):
    return LifecycleEvent(
        finding_id=finding_id("code", title),
        state=state,
        title=title,
        loop="code",
        risk="low",
        pr=pr,
        paths=paths,
    )


def test_an_open_candidate_reports_the_files_it_edits(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("armed", title="Retry defaults forked", paths=("src/retry.py",)))

    (change,) = ledger.open_changes()

    assert change.title == "Retry defaults forked"
    assert change.paths == ("src/retry.py",)


def test_a_parked_candidate_is_an_obstacle_too(tmp_path: Path) -> None:
    """A draft awaiting a person still holds a branch, and still conflicts."""
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("parked", title="Status vocabulary forked", paths=("src/api.py",)))

    assert [change.paths for change in ledger.open_changes()] == [("src/api.py",)]


def test_a_merged_candidate_is_not_an_obstacle(tmp_path: Path) -> None:
    """Its edits are in the base branch the next session starts from, so there
    is nothing left to collide with — and listing it would steer sessions away
    from files that are perfectly free."""
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("armed", title="Retry defaults forked", paths=("src/retry.py",)))
    ledger.append(_event("merged", title="Retry defaults forked", paths=("src/retry.py",)))

    assert ledger.open_changes() == []


def test_a_row_written_before_this_field_existed_claims_no_files(tmp_path: Path) -> None:
    """Older ledgers are read, not migrated. A row with no paths must not read
    as a candidate that edits nothing — it reads as one that never said."""
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("armed", title="Old finding", paths=()))

    assert ledger.projections()[finding_id("code", "Old finding")].paths == ()
    assert ledger.open_changes() == []


def test_a_malformed_path_list_is_discarded_rather_than_trusted(tmp_path: Path) -> None:
    """A bare string is iterable, so trusting the shape would spell one path
    into a list of single characters and route every session around 's', 'r', 'c'."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"state": "armed", "title": "Bad row", "loop": "code", "paths": "src/api.py"}\n',
        encoding="utf-8",
    )

    assert Ledger(path).open_changes() == []


def test_no_open_candidate_adds_nothing_to_the_brief() -> None:
    context = SimpleNamespace(ledger=SimpleNamespace(open_changes=lambda: []))
    assert _open_changes(context) == ""


def test_the_brief_names_the_pull_request_and_every_file_it_holds() -> None:
    change = SimpleNamespace(pr=2, title="Status vocabulary forked", paths=("src/api.py", "t.py"))
    context = SimpleNamespace(ledger=SimpleNamespace(open_changes=lambda: [change]))

    text = _open_changes(context)

    assert "## Files an open candidate already edits" in text
    assert "#2 Status vocabulary forked" in text
    assert "`src/api.py`" in text
    assert "`t.py`" in text


def test_the_brief_says_why_those_files_matter() -> None:
    """A bare list reads as trivia. The session has to be told the edits are not
    in its worktree, so git will not warn it the way it warns about everything else."""
    change = SimpleNamespace(pr=None, title="Some finding", paths=("src/api.py",))
    context = SimpleNamespace(ledger=SimpleNamespace(open_changes=lambda: [change]))

    text = _open_changes(context)

    assert "unmerged" in text
    assert "conflicts" in text
    assert "Prefer a defect elsewhere" in text


class _Executor:
    def __init__(self, stdout: str) -> None:
        self._stdout = stdout

    def run(self, argv, *, cwd=None, timeout=None, env=None):  # type: ignore[no-untyped-def]
        return Result(code=0, stdout=self._stdout, stderr="")

    def exists(self, path: str) -> bool:  # pragma: no cover - no .md path in these tests
        return False


def _classify_context(stdout: str, **loop_fields):  # type: ignore[no-untyped-def]
    loop = SimpleNamespace(
        require_change_under=(), confine_to=(), protected_paths=(), **loop_fields
    )
    return SimpleNamespace(
        executor=_Executor(stdout),
        config=SimpleNamespace(forge=SimpleNamespace(default_branch="main")),
        loop=lambda name: loop,
    )


def test_classify_reports_the_files_it_measured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Publish writes the ledger row, and it has no worktree of its own to
    measure. The measurement classify already made is the only one that matches
    the diff being published."""
    context = _classify_context(" M src/retry.py\n?? tests/test_retry.py\n")
    monkeypatch.setattr(classify, "current", lambda: context)

    result = classify.run({"loop": "code", "worktree": "/tree", "finding": {"risk": "low"}})

    assert result["risk"] == "low"
    assert result["changed_paths"] == ["src/retry.py", "tests/test_retry.py"]


def test_an_escalated_candidate_still_reports_its_files(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """It opens a draft rather than a ready pull request, and a draft holds its
    branch exactly as long. Omitting these would leave the loop steering fresh
    sessions straight into the one change a person has not looked at yet."""
    context = _classify_context(" M .github/workflows/ci.yml\n")
    monkeypatch.setattr(classify, "current", lambda: context)

    result = classify.run({"loop": "code", "worktree": "/tree", "finding": {"risk": "low"}})

    assert result["risk"] == "high"
    assert "protected path" in result["escalation"]
    assert result["changed_paths"] == [".github/workflows/ci.yml"]


def _publish_context():  # type: ignore[no-untyped-def]
    """The real GitConfig rather than a stand-in: `_request` reads it through a
    method, and a hand-rolled double stops matching the moment one is added."""
    from touchstone.config import GitConfig

    return SimpleNamespace(
        config=SimpleNamespace(
            forge=SimpleNamespace(
                default_branch="main",
                escalation_label="needs-review",
                slug="acme/widgets",
                required_workflows=("ci.yml",),
            ),
            git=GitConfig(author_name="acme[bot]", author_email="bot@example.invalid"),
        ),
        loop=lambda _name: SimpleNamespace(name="code", label="touchstone:audit", auto_merge=False),
    )


def test_publish_carries_the_measurement_into_the_request() -> None:
    """The seam between the two nodes. classify measures against the worktree;
    publish is what writes the row, and a row that forgets the paths is a
    candidate that never announces itself."""
    from touchstone.nodes.publish import _request

    state = {
        "loop": "code",
        "branch": "touchstone/abc",
        "worktree": "/tree",
        "finding": {"title": "Retry defaults forked", "risk": "low"},
        "changed_paths": ["src/retry.py", "tests/test_retry.py"],
    }

    request = _request(state, _publish_context())

    assert request.paths == ("src/retry.py", "tests/test_retry.py")


def test_a_run_that_measured_nothing_requests_no_paths() -> None:
    """classify returns no measurement when it escalated before taking one.
    That has to reach the row as "said nothing", never as "edits nothing"."""
    from touchstone.nodes.publish import _request

    request = _request(
        {"loop": "code", "branch": "b", "worktree": "/tree", "finding": {}},
        _publish_context(),
    )

    assert request.paths == ()
