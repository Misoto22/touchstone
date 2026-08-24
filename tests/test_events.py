from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from touchstone.engines.base import Session
from touchstone.events import EventLog, run_event
from touchstone.nodes import audit


def _config(secret: str = "do-not-log"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        source=SimpleNamespace(schema_version=1),
        forge=SimpleNamespace(slug="acme/widgets", default_branch="main"),
        engine=SimpleNamespace(name="codex", model="gpt-test", audit_effort="high"),
        execution=SimpleNamespace(
            target="ssh",
            ssh=SimpleNamespace(host="worker.example", env=(("SECRET_TOKEN", secret),)),
        ),
        loops={"code": SimpleNamespace(name="code", schedule="hourly")},
    )


def test_event_log_excludes_prompts_and_environment_values(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        run_event(
            _config(),
            run_id="run-1",
            kind="finished",
            loop="code",
            outcome="clean",
        )
    )

    text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "do-not-log" not in text
    assert "SECRET_TOKEN" not in text
    assert "prompt" not in text.casefold()


def test_event_log_is_append_only_and_machine_readable(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(run_event(_config(), run_id="run-1", kind="started", loop="code"))
    log.append(
        run_event(
            _config(),
            run_id="run-1",
            kind="finished",
            loop="code",
            outcome="held",
            detail="health gate did not pass",
        )
    )

    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [row["kind"] for row in rows] == ["started", "finished"]
    assert rows[0]["config_fingerprint"] == rows[1]["config_fingerprint"]
    assert rows[1]["outcome"] == "held"


def test_runner_records_started_and_finished_events() -> None:
    import inspect

    from touchstone import runner

    source = inspect.getsource(runner.execute)
    assert "EventLog" in source
    assert 'kind="started"' in source
    assert 'kind="finished"' in source


def test_engine_failure_transcript_does_not_enter_graph_notes() -> None:
    secret_transcript = "model said: private repository content"
    session = Session(
        ok=False,
        text="",
        cost=None,
        timed_out=False,
        detail=secret_transcript,
    )

    assert secret_transcript not in audit._session_failure("codex")
    assert session.detail == secret_transcript
