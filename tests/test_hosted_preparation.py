from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from touchstone.config import ConfigError, load
from touchstone.discovery import ProjectDiscovery
from touchstone.execution.local import LocalExecutor
from touchstone.hosted.runtime import (
    CandidateIntegrityError,
    PreparationAttestation,
    _reuse_prepared_dependencies,
    install_stage,
)
from touchstone.hosted.snapshot import config_digest
from touchstone.initialize import InitOptions, initialize


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "app"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"name":"app","scripts":{"test":"true"}}', encoding="utf-8"
    )
    (repository / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (repository / "src").mkdir()
    (repository / "src" / "index.js").write_text("export default 1;\n", encoding="utf-8")
    (repository / ".gitignore").write_text("node_modules/\n.touchstone/\n", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.test")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "seed")
    return repository


def _config(repository: Path):  # type: ignore[no-untyped-def]
    report = initialize(
        InitOptions(
            start=repository,
            engine="codex",
            model="model-test",
            workflows=("ci.yml",),
            schedule="hourly@00",
            discovered=ProjectDiscovery(repository, "acme/app", "main", ("codex",), "launchd"),
        ),
        LocalExecutor(),
    )
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "configure")
    return load(report.root)


def _head(repository: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _worktree(repository: Path, destination: Path) -> Path:
    _git(repository, "worktree", "add", "--detach", str(destination), "HEAD")
    return destination


def test_install_stage_refuses_to_run_beside_any_credential(tmp_path: Path) -> None:
    config = _config(_repository(tmp_path))

    with pytest.raises(CandidateIntegrityError, match="prohibited credential"):
        install_stage(config, for_stage="analysis", env={"OPENAI_API_KEY": "model-secret"})
    with pytest.raises(CandidateIntegrityError, match="prohibited credential"):
        install_stage(config, for_stage="verify", env={"TOUCHSTONE_STATE_KEY": "state"})
    with pytest.raises(CandidateIntegrityError, match="prohibited credential"):
        install_stage(config, for_stage="verify", env={"GH_TOKEN": "token"})


def test_install_stage_binds_the_attestation_to_head_config_targets_and_lockfiles(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)

    attestation = install_stage(config, for_stage="verify", env={})

    assert attestation is not None
    assert attestation.head_sha == _head(repository)
    assert attestation.config_digest == config_digest(config)
    assert attestation.targets == tuple(sorted(config.targets))
    assert dict(attestation.lockfiles)["package-lock.json"].startswith("sha256:")
    stored = json.loads(
        (repository / ".touchstone" / "hosted" / "install" / "preparation.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["head_sha"] == attestation.head_sha
    assert stored["outcome"] == "completed"


def test_install_stage_does_nothing_for_stages_that_never_validate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)

    assert install_stage(config, for_stage="publish", env={}) is None
    assert install_stage(config, for_stage="snapshot", env={}) is None
    assert not (repository / ".touchstone" / "hosted" / "install").exists()


def test_analysis_fails_closed_when_no_attestation_exists_beside_a_model_credential(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    worktree = _worktree(repository, tmp_path / "analysis-worktree")

    reason = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={"OPENAI_API_KEY": "model-secret"},
    )

    assert reason == "preparation-attestation-missing"


def test_analysis_reuses_the_exact_credential_free_preparation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    modules = repository / "node_modules"
    modules.mkdir()
    (modules / "marker.txt").write_text("prepared\n", encoding="utf-8")
    install_stage(config, for_stage="verify", env={})
    worktree = _worktree(repository, tmp_path / "analysis-worktree")

    reason = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={"OPENAI_API_KEY": "model-secret"},
    )

    assert reason == ""
    assert (worktree / "node_modules" / "marker.txt").read_text(encoding="utf-8") == "prepared\n"


def test_a_stale_attestation_fails_closed_beside_a_model_credential(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    install_stage(config, for_stage="verify", env={})
    (repository / "src" / "extra.js").write_text("export default 2;\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "move HEAD")
    worktree = _worktree(repository, tmp_path / "analysis-worktree")

    reason = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={"OPENAI_API_KEY": "model-secret"},
    )

    assert reason == "preparation-head-mismatch"


def test_a_changed_lockfile_fails_closed_beside_a_model_credential(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    install_stage(config, for_stage="verify", env={})
    worktree = _worktree(repository, tmp_path / "analysis-worktree")
    (worktree / "package-lock.json").write_text('{"lockfileVersion":4}\n', encoding="utf-8")

    reason = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={"OPENAI_API_KEY": "model-secret"},
    )

    assert reason == "preparation-lockfile-mismatch"


def test_a_credential_free_process_may_still_prepare_inline(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    worktree = _worktree(repository, tmp_path / "analysis-worktree")

    reason = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={},
    )

    assert reason == ""


def test_publish_never_prepares_dependencies_inline(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    worktree = _worktree(repository, tmp_path / "publish-worktree")

    reason = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="publish",
        targets=config.loop("code").targets,
        env={},
    )

    assert reason == "preparation-attestation-missing"


def test_a_blocked_preparation_stops_the_credential_free_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    repository = _repository(tmp_path)
    config = _config(repository)
    monkeypatch.setattr(
        "touchstone.validation.prepare",
        lambda *_args, **_kwargs: SimpleNamespace(
            outcome="blocked",
            results=(
                SimpleNamespace(argv=("poetry", "install"), reason="policy-unsupported", ok=False),
            ),
        ),
    )

    with pytest.raises(ConfigError, match="before credentials"):
        install_stage(config, for_stage="verify", env={})


def test_an_attestation_directory_cannot_escape_the_repository(tmp_path: Path) -> None:
    with pytest.raises(CandidateIntegrityError, match="escapes the repo"):
        PreparationAttestation(
            head_sha="a" * 40,
            config_digest="sha256:" + "b" * 64,
            targets=("app",),
            lockfiles=(),
            directories=("../outside",),
            outcome="completed",
        ).validate()


def test_a_corrupt_attestation_fails_closed_but_stays_recoverable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config = _config(repository)
    install_stage(config, for_stage="verify", env={})
    (repository / ".touchstone" / "hosted" / "install" / "preparation.json").write_text(
        "not json at all", encoding="utf-8"
    )
    worktree = _worktree(repository, tmp_path / "analysis-worktree")

    with_credential = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={"OPENAI_API_KEY": "model-secret"},
    )
    without_credential = _reuse_prepared_dependencies(
        config,
        worktree,
        stage="analysis",
        targets=config.loop("code").targets,
        env={},
    )

    assert with_credential == "preparation-attestation-invalid"
    assert without_credential == ""


def test_the_analysis_install_also_prepares_the_agent_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the analysis stage installs the Agent CLI; every stage prepares the project."""
    from touchstone.hosted import runtime

    repository = _repository(tmp_path)
    config = _config(repository)
    calls: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_ensure_engine",
        lambda _config, _env, allow_install=False: calls.append(f"install={allow_install}"),
    )

    analysis = install_stage(config, for_stage="analysis", env={})
    calls_after_analysis = list(calls)
    verify = install_stage(config, for_stage="verify", env={})

    assert calls_after_analysis == ["install=True"]
    # Verify prepares the project without touching the Agent runtime.
    assert calls == calls_after_analysis
    assert analysis is not None and verify is not None
    assert analysis.head_sha == verify.head_sha
