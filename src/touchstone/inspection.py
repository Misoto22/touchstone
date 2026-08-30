"""Read-only inspection of configuration values and their owners."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from touchstone.config import Config, ConfigError, load
from touchstone.config_v2 import merge_generated
from touchstone.harnesses import registry_path


@dataclass(frozen=True, slots=True)
class _Layer:
    path: Path
    values: dict[str, Any]


def configuration_paths(config: Config | Path) -> dict[str, Path]:
    """Name every file that can own an effective project value."""

    loaded = load(config) if isinstance(config, Path) else config
    result = {"project": loaded.source.path, "harness_registry": registry_path()}
    if loaded.source.generated_path is not None:
        result["generated"] = loaded.source.generated_path
    raw = _read_toml(loaded.source.path)
    reference = raw.get("extends")
    if isinstance(reference, str) and reference:
        candidate = (loaded.source.path.parent / reference).resolve()
        if candidate.is_relative_to(loaded.source.path.parent.resolve()):
            result["fleet"] = candidate
    return result


def redacted_configuration(config: Config | Path) -> dict[str, Any]:
    """Return the merged tracked configuration with defensive redaction."""

    layers = _layers(config)
    merged: dict[str, Any] = {}
    for layer in layers:
        merged = merge_generated(merged, layer.values)
    return _redact(merged)


def effective_configuration(config: Config | Path) -> dict[str, dict[str, Any]]:
    """Flatten effective values and retain the last owning file for each."""

    loaded = load(config) if isinstance(config, Path) else config
    layers = _layers(loaded)
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for layer in layers:
        merged = merge_generated(merged, layer.values)
        for field in _flatten(layer.values):
            provenance[field] = _display_path(layer.path, layers[-1].path.parent)
    values = _flatten(_redact(merged))
    overrides = {
        "TOUCHSTONE_ENGINE": ("engine.name", loaded.engine.name),
        "TOUCHSTONE_MODEL": ("engine.model", loaded.engine.model),
        "TOUCHSTONE_SANDBOX": ("engine.sandbox", loaded.engine.sandbox),
        "TOUCHSTONE_EFFORT": ("engine.audit_effort", loaded.engine.audit_effort),
        "TOUCHSTONE_REVIEW_EFFORT": (
            "engine.review_effort",
            loaded.engine.review_effort,
        ),
        "TOUCHSTONE_TIMEOUT": ("engine.timeout_seconds", loaded.engine.timeout_seconds),
        "TOUCHSTONE_TARGET": ("execution.target", loaded.execution.target),
        "TOUCHSTONE_REPO": ("project.path", str(loaded.repo_path)),
        "TOUCHSTONE_STATE": ("state_dir", str(loaded.state_dir)),
    }
    for variable, (field, value) in overrides.items():
        if variable in os.environ:
            values[field] = value
            provenance[field] = f"environment:{variable}"
    return {
        field: {"value": value, "source": provenance.get(field, "default")}
        for field, value in sorted(values.items())
    }


def explain_configuration_field(config: Config | Path, field: str) -> dict[str, Any]:
    effective = effective_configuration(config)
    try:
        record = effective[field]
    except KeyError:
        raise ConfigError(f"configuration has no effective field {field!r}") from None
    return {"field": field, **record}


def _layers(config: Config | Path) -> tuple[_Layer, ...]:
    loaded = load(config) if isinstance(config, Path) else config
    paths = configuration_paths(loaded)
    layers: list[_Layer] = []
    if "generated" in paths:
        layers.append(_Layer(paths["generated"], _read_toml(paths["generated"])))
    if "fleet" in paths:
        layers.append(_Layer(paths["fleet"], _read_toml(paths["fleet"])))
    layers.append(_Layer(paths["project"], _read_toml(paths["project"])))
    return tuple(layers)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"configuration source does not exist: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, child in value.items():
        field = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict):
            result.update(_flatten(child, field))
        else:
            result[field] = child
    return result


def _redact(value: Any, key: str = "") -> Any:
    marker = key.upper()
    if key != "api_key_ref" and any(
        sensitive in marker
        for sensitive in ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "PRIVATE_KEY")
    ):
        return "<redacted>"
    if isinstance(value, dict):
        return {child_key: _redact(child, child_key) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, key) for child in value]
    return value


def _display_path(path: Path, repository: Path) -> str:
    try:
        return path.resolve().relative_to(repository.resolve()).as_posix()
    except ValueError:
        return os.fspath(path.resolve())


__all__ = [
    "configuration_paths",
    "effective_configuration",
    "explain_configuration_field",
    "redacted_configuration",
]
