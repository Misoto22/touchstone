"""The repository State Snapshot outlives an edit to touchstone.toml.

Adding a Loop changed the configuration digest, and the digest was checked on
restore. The next run therefore began with an empty ledger, rediscovered the
defects it had already proposed, and opened a second pull request for each —
with every stage green and one word in a log the only sign it had happened.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from touchstone.hosted.snapshot import compatibility, snapshot_state
from touchstone.outcomes import RunOutcome, RunResult

REPOSITORY = "__repository__"


def _config(tmp_path: Path, **overrides):  # type: ignore[no-untyped-def]
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    fields = {
        "state_dir": state,
        "source": SimpleNamespace(schema_version=2),
        "forge": SimpleNamespace(slug="acme/widgets"),
        "generated_metadata": SimpleNamespace(source_digest="profile-digest"),
        "loops": {"code": SimpleNamespace(name="code", brief="builtin:code-audit")},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _manifest(config, *, loop: str = REPOSITORY):  # type: ignore[no-untyped-def]
    return snapshot_state(
        config,
        RunResult(RunOutcome.NO_CHANGE),
        loop=loop,
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    ).manifest


def test_the_repository_snapshot_survives_a_new_loop(tmp_path: Path) -> None:
    written = _manifest(_config(tmp_path))
    after = _config(
        tmp_path,
        loops={
            "code": SimpleNamespace(name="code", brief="builtin:code-audit"),
            "naming": SimpleNamespace(name="naming", brief="builtin:naming"),
        },
    )

    checked = compatibility(
        written, after, loop=REPOSITORY, lineage=None, require_configuration=False
    )

    assert checked.ok, checked.clean_start_reason


def test_a_new_target_does_not_discard_the_ledger(tmp_path: Path) -> None:
    """`profile refresh` moves the source digest whenever a Target or a Profile
    key changes. The ledger records pull requests, not Targets."""
    written = _manifest(_config(tmp_path))
    after = _config(tmp_path, generated_metadata=SimpleNamespace(source_digest="another-digest"))

    checked = compatibility(
        written, after, loop=REPOSITORY, lineage=None, require_configuration=False
    )

    assert checked.ok, checked.clean_start_reason


def test_a_candidate_still_refuses_a_changed_configuration(tmp_path: Path) -> None:
    """A candidate was analysed against one set of Loops, protected paths and
    Targets. Publishing it under another ships work nobody authorised, so the
    strict check stays the default and stays on for candidates."""
    written = _manifest(_config(tmp_path), loop="code")
    after = _config(
        tmp_path,
        loops={
            "code": SimpleNamespace(name="code", brief="builtin:code-audit"),
            "naming": SimpleNamespace(name="naming", brief="builtin:naming"),
        },
    )

    checked = compatibility(written, after, loop="code", lineage=None)

    assert checked.ok is False
    assert checked.clean_start_reason == "config-mismatch"


def test_the_repository_snapshot_still_refuses_another_repository(tmp_path: Path) -> None:
    written = _manifest(_config(tmp_path))
    after = _config(tmp_path, forge=SimpleNamespace(slug="acme/other"))

    checked = compatibility(
        written, after, loop=REPOSITORY, lineage=None, require_configuration=False
    )

    assert checked.ok is False
    assert checked.clean_start_reason == "repository-mismatch"


def test_the_repository_snapshot_still_refuses_another_schema(tmp_path: Path) -> None:
    """The state layout is the schema's, so a bundle from another one cannot be
    read even though its contents describe the same repository."""
    written = _manifest(_config(tmp_path))
    after = _config(tmp_path, source=SimpleNamespace(schema_version=3))

    checked = compatibility(
        written, after, loop=REPOSITORY, lineage=None, require_configuration=False
    )

    assert checked.ok is False
    assert checked.clean_start_reason == "schema-mismatch"


def _prepare(monkeypatch, tmp_path: Path, results: list[str]):  # type: ignore[no-untyped-def]
    """Drive prepare with the artifact lookup stubbed, and report every call."""
    from touchstone.hosted import runtime

    calls: list[str | None] = []

    def _fake(config, env, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs.get("artifact_name"))
        kwargs["destination"].parent.mkdir(parents=True, exist_ok=True)
        return results.pop(0)

    monkeypatch.setattr(runtime, "_download_artifact_file", _fake)
    monkeypatch.setattr(runtime, "_state_artifact_name", lambda config: "touchstone-state-new")
    output = runtime._prepare_stage(
        _config(tmp_path), tmp_path / "root", "run-1", {"GITHUB_REPOSITORY": "acme/widgets"}
    )
    return output, calls


def test_a_renamed_bundle_is_found_under_the_prefix(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The name is written into the workflow by `actions init` and recomputed
    at run time, so editing the configuration renames what the next run looks
    for while the workflow still uploads under the old one."""
    output, calls = _prepare(monkeypatch, tmp_path, ["artifact-not-found", ""])

    assert calls == ["touchstone-state-new", None], "the retry must not filter by name"
    assert output.clean_start_reason == "", "a recovered bundle is not a clean start"
    assert "actions init" in output.state_note


def test_a_repository_with_no_bundle_at_all_still_starts_clean(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """First run of a new repository. Nothing was lost, so nothing is said."""
    output, calls = _prepare(monkeypatch, tmp_path, ["artifact-not-found", "artifact-not-found"])

    assert len(calls) == 2
    assert output.clean_start_reason == "artifact-not-found"
    assert output.state_note == ""


def test_a_bundle_found_by_name_is_not_looked_for_twice(monkeypatch, tmp_path: Path) -> None:
    output, calls = _prepare(monkeypatch, tmp_path, [""])

    assert calls == ["touchstone-state-new"]
    assert output.clean_start_reason == ""
    assert output.state_note == ""
