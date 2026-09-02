from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

from touchstone.cli import main
from touchstone.harnesses import HarnessContext
from touchstone.inspection import configuration_paths, effective_configuration


def _configured_repository(tmp_path: Path, *, entrypoint: bool = True) -> Path:
    (tmp_path / ".touchstone").mkdir()
    (tmp_path / ".touchstone/generated.toml").write_text(
        """\
[metadata]
package_version = "0.1.2"
source_digest = "sha256:test"
[metadata.profile_versions]
""",
        encoding="utf-8",
    )
    (tmp_path / ".touchstone/fleet.toml").write_text(
        """\
[engine]
name = "codex"
model = "gpt-test"
[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
[loop.code.context]
OPENAI_API_KEY = "sk-live-must-not-appear"
ANTHROPIC_API_KEY = "sk-ant-must-not-appear"
API_KEY = "bare-must-not-appear"
CREDENTIAL = "cred-must-not-appear"
PASSPHRASE = "phrase-must-not-appear"
GITHUB_TOKEN = "tok-must-not-appear"
region = "au"
""",
        encoding="utf-8",
    )
    config = tmp_path / "touchstone.toml"
    config.write_text(
        """\
version = 2
generated = ".touchstone/generated.toml"
extends = ".touchstone/fleet.toml"
timezone = "Australia/Sydney"
[project]
path = "."
[forge]
slug = "acme/widgets"
[harness]
mode = "embedded"
entrypoint = "AGENTS.md"
""",
        encoding="utf-8",
    )
    if entrypoint:
        (tmp_path / "AGENTS.md").write_text("repository rules\n", encoding="utf-8")
    # `config check` reads the revision a run would build from, so the fixture needs one.
    _commit_as_origin_main(tmp_path)
    return config


def _commit_as_origin_main(root: Path) -> None:
    def run(*argv: str) -> None:
        subprocess.run(argv, cwd=root, check=True, capture_output=True, text=True)

    run("git", "init", "-b", "main")
    run("git", "config", "user.name", "Test")
    run("git", "config", "user.email", "test@example.invalid")
    run("git", "add", "-A")
    run("git", "commit", "-m", "fixture")
    run("git", "update-ref", "refs/remotes/origin/main", "HEAD")


def test_configuration_paths_names_each_owner(tmp_path: Path) -> None:
    paths = configuration_paths(_configured_repository(tmp_path))

    assert paths["project"] == (tmp_path / "touchstone.toml").resolve()
    assert paths["generated"] == (tmp_path / ".touchstone/generated.toml").resolve()
    assert paths["fleet"] == (tmp_path / ".touchstone/fleet.toml").resolve()
    assert paths["harness_registry"].name == "harnesses.toml"


def test_effective_configuration_reports_field_owner(tmp_path: Path) -> None:
    effective = effective_configuration(_configured_repository(tmp_path))

    assert effective["harness.mode"] == {
        "value": "embedded",
        "source": "touchstone.toml",
    }
    assert effective["engine.model"] == {
        "value": "gpt-test",
        "source": ".touchstone/fleet.toml",
    }
    assert effective["metadata.source_digest"] == {
        "value": "sha256:test",
        "source": ".touchstone/generated.toml",
    }


def test_effective_configuration_reports_environment_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)
    monkeypatch.setenv("TOUCHSTONE_MODEL", "gpt-override")

    effective = effective_configuration(config)

    assert effective["engine.model"] == {
        "value": "gpt-override",
        "source": "environment:TOUCHSTONE_MODEL",
    }


def test_config_show_effective_json_uses_public_boundary(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "config", "show", "--effective", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["harness.mode"]["value"] == "embedded"


def test_config_check_returns_typed_blocked_result_for_missing_harness_entrypoint(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path, entrypoint=False)

    assert main(["--config", str(config), "config", "check", "--json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "blocked",
        "reason": "harness-entrypoint-missing",
        "detail": "Harness entrypoint is not a regular file at origin/main: AGENTS.md",
    }


def test_harness_resolve_json_does_not_expose_machine_snapshot_path(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "harness", "resolve", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["entrypoint"] == "AGENTS.md"
    assert str(tmp_path) not in json.dumps(payload)


def test_harness_resolve_removes_external_snapshot_after_inspection(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)
    snapshot = tmp_path / "snapshot-test"
    entrypoint = snapshot / "harness/00-INDEX.md"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("rules\n", encoding="utf-8")
    context = HarnessContext(
        mode="external",
        source="acme/harness",
        entrypoint=entrypoint,
        revision="abc123",
        context_root=snapshot,
        evidence=("ref:origin/main",),
    )
    monkeypatch.setattr("touchstone.harnesses.resolve_harness", lambda *_args, **_kwargs: context)

    assert main(["--config", str(config), "harness", "resolve", "--json"]) == 0
    capsys.readouterr()

    assert not snapshot.exists()


def test_harness_registry_commands_mutate_only_the_local_registry(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    xdg = tmp_path / "xdg"
    checkout = tmp_path / "efficient-harness"
    checkout.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert (
        main(
            [
                "harness",
                "register",
                "Efficient-Pty-Ltd/efficient-harness",
                "--path",
                str(checkout),
            ]
        )
        == 0
    )
    registry = xdg / "touchstone/harnesses.toml"
    raw = tomllib.loads(registry.read_text(encoding="utf-8"))
    assert raw == {
        "harnesses": {"Efficient-Pty-Ltd/efficient-harness": {"path": str(checkout.resolve())}}
    }
    assert main(["harness", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert listed == {"Efficient-Pty-Ltd/efficient-harness": str(checkout.resolve())}

    assert main(["harness", "unregister", "Efficient-Pty-Ltd/efficient-harness"]) == 0
    assert tomllib.loads(registry.read_text(encoding="utf-8")) == {"harnesses": {}}


def test_config_explain_reports_one_effective_field(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "config", "explain", "harness.mode", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "field": "harness.mode",
        "value": "embedded",
        "source": "touchstone.toml",
    }


def test_effective_configuration_redacts_secret_shaped_context(tmp_path: Path) -> None:
    effective = effective_configuration(_configured_repository(tmp_path))

    for field in (
        "loop.code.context.OPENAI_API_KEY",
        "loop.code.context.ANTHROPIC_API_KEY",
        "loop.code.context.API_KEY",
        "loop.code.context.CREDENTIAL",
        "loop.code.context.PASSPHRASE",
        "loop.code.context.GITHUB_TOKEN",
    ):
        assert effective[field]["value"] == "<redacted>", field
    assert effective["loop.code.context.region"]["value"] == "au"


def test_config_show_never_prints_a_secret_shaped_value(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "config", "show", "--json"]) == 0

    printed = capsys.readouterr().out
    for secret in (
        "sk-live-must-not-appear",
        "sk-ant-must-not-appear",
        "bare-must-not-appear",
        "cred-must-not-appear",
        "phrase-must-not-appear",
        "tok-must-not-appear",
    ):
        assert secret not in printed, secret
    assert "<redacted>" in printed


def test_effective_configuration_includes_runtime_defaults(tmp_path: Path) -> None:
    """A field no file sets still has a value at run time, and must be shown as one."""
    config = _configured_repository(tmp_path)
    raw = tomllib.loads((tmp_path / "touchstone.toml").read_text(encoding="utf-8"))
    assert "sandbox" not in raw.get("engine", {})
    assert "execution" not in raw

    effective = effective_configuration(config)

    for field in ("engine.sandbox", "engine.timeout_seconds", "execution.target"):
        assert field in effective, field
        assert effective[field]["source"] == "default"
    # a value a file does set keeps its owner
    assert effective["engine.model"]["source"] == ".touchstone/fleet.toml"


def test_config_explain_reports_a_defaulted_field(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "config", "explain", "engine.sandbox", "--json"]) == 0

    explained = json.loads(capsys.readouterr().out)
    assert explained["field"] == "engine.sandbox"
    assert explained["source"] == "default"


def test_config_check_reads_the_revision_a_run_would_build_from(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """A feature branch carrying the entrypoint used to mask a base branch without it."""
    config = _configured_repository(tmp_path, entrypoint=False)
    # The working tree gains the entrypoint, but `origin/main` never does.
    (tmp_path / "AGENTS.md").write_text("only on this branch\n", encoding="utf-8")

    assert main(["--config", str(config), "config", "check", "--json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "harness-entrypoint-missing"
    assert "origin/main" in payload["detail"]
