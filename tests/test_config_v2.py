from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w

from touchstone.config import ConfigError, load


def _write(path: Path, data: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")
    return path


def _root_config(*, generated: str = ".touchstone/generated.toml") -> dict[str, object]:
    return {
        "version": 2,
        "generated": generated,
        "timezone": "Australia/Sydney",
        "project": {"path": "."},
        "forge": {
            "slug": "acme/widgets",
            "required_workflows": ["ci.yml", "security.yml"],
        },
        "engine": {"name": "codex", "model": "gpt-test", "timeout_seconds": 1200},
        "actions": {
            "visibility": "private",
            "wake_minutes": 60,
            "artifact_retention_days": 90,
            "auto_merge": False,
        },
        "loop": {
            "code": {
                "brief": "builtin:code-audit",
                "label": "touchstone:audit",
                "schedule": "hourly@00",
                "targets": ["web"],
                "priority": 10,
            }
        },
    }


def _generated_config() -> dict[str, object]:
    return {
        "metadata": {
            "package_version": "0.1.2",
            "profile_versions": {"javascript": "1", "nextjs": "1"},
            "source_digest": "sha256:test",
        },
        "forge": {"required_workflows": ["ci.yml"]},
        "engine": {"timeout_seconds": 900},
        "target": {
            "web": {
                "path": "apps/web",
                "profiles": ["javascript", "nextjs"],
                "dependencies": [],
                "validation": [
                    {
                        "argv": ["npm", "test"],
                        "timeout_seconds": 600,
                        "capability": "package-script",
                        "enabled": False,
                    }
                ],
            }
        },
    }


def test_v1_still_loads_without_generated_file(tmp_path: Path) -> None:
    path = tmp_path / "touchstone.toml"
    path.write_text(
        """\
version = 1
[project]
path = "."
[forge]
slug = "acme/widgets"
[engine]
name = "codex"
model = "gpt-test"
[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
""",
        encoding="utf-8",
    )

    assert load(path).source.schema_version == 1


def test_v2_loads_generated_then_project_override(tmp_path: Path) -> None:
    generated = _write(tmp_path / ".touchstone/generated.toml", _generated_config())
    root = _write(tmp_path / "touchstone.toml", _root_config())

    config = load(root)

    assert config.source.schema_version == 2
    assert config.source.generated_path == generated.resolve()
    assert config.timezone == "Australia/Sydney"
    assert config.engine.timeout_seconds == 1200
    assert config.forge.required_workflows == ("ci.yml", "security.yml")
    assert config.targets["web"].path == Path("apps/web")
    assert config.targets["web"].profiles == ("javascript", "nextjs")
    assert config.targets["web"].validation[0].argv == ("npm", "test")
    assert config.targets["web"].validation[0].enabled is False
    assert config.loop("code").targets == ("web",)
    assert config.loop("code").priority == 10
    assert config.generated_metadata is not None
    assert config.actions.visibility == "private"
    assert config.actions.wake_minutes == 60
    assert config.actions.auto_merge is False
    assert config.generated_metadata.source_digest == "sha256:test"


def test_generated_path_cannot_escape_repository(tmp_path: Path) -> None:
    _write(tmp_path.parent / "outside.toml", _generated_config())
    root = _write(tmp_path / "touchstone.toml", _root_config(generated="../outside.toml"))

    with pytest.raises(ConfigError, match=r"generated.*repository"):
        load(root)


def test_project_can_enable_a_generated_validation_candidate(tmp_path: Path) -> None:
    _write(tmp_path / ".touchstone/generated.toml", _generated_config())
    root_data = _root_config()
    root_data["target"] = {"web": {"validation": [{"argv": ["npm", "test"], "enabled": True}]}}
    root = _write(tmp_path / "touchstone.toml", root_data)

    config = load(root)

    assert len(config.targets["web"].validation) == 1
    assert config.targets["web"].validation[0].enabled is True
