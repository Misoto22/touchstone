"""Versioned, project-neutral Touchstone configuration."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from string import Template
from typing import Any, Literal

EngineName = Literal["codex", "claude"]
Target = Literal["local", "ssh"]


class ConfigError(ValueError):
    """The configuration is unusable, and the run should not start."""


@dataclass(frozen=True, slots=True)
class ConfigSource:
    path: Path
    schema_version: int


@dataclass(frozen=True, slots=True)
class Budget:
    audit: float = 20.0
    review: float = 4.0


@dataclass(frozen=True, slots=True)
class EngineConfig:
    name: EngineName = "codex"
    model: str = ""
    audit_effort: str = "high"
    review_effort: str = "high"
    timeout_seconds: int = 2700
    budget: Budget = field(default_factory=Budget)
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SshConfig:
    host: str
    workdir: str
    state_dir: str
    env: tuple[tuple[str, str], ...] = ()
    identity_file: str | None = None
    connect_timeout: int = 15

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ConfigError("execution.ssh.host must not be empty")
        if not PurePosixPath(self.workdir).is_absolute():
            raise ConfigError("execution.ssh.workdir must be an absolute remote path")
        if not PurePosixPath(self.state_dir).is_absolute():
            raise ConfigError("execution.ssh.state_dir must be an absolute remote path")
        secret_markers = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "PRIVATE_KEY")
        for key, _value in self.env:
            normalized = key.upper()
            if any(marker in normalized for marker in secret_markers):
                raise ConfigError(
                    f"execution.ssh.env contains secret-like key {key!r}; "
                    "provide credentials through the remote runtime instead"
                )


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    target: Target = "local"
    ssh: SshConfig | None = None

    def __post_init__(self) -> None:
        if self.target == "ssh" and self.ssh is None:
            raise ConfigError("execution.target is 'ssh' but no [execution.ssh] section was given")


@dataclass(frozen=True, slots=True)
class GitConfig:
    author_name: str | None = None
    author_email: str | None = None

    def __post_init__(self) -> None:
        if bool(self.author_name) != bool(self.author_email):
            raise ConfigError("git.author_name and git.author_email must be set together")


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    slug: str = ""
    provider: Literal["github"] = "github"
    default_branch: str = "main"
    escalation_label: str = "touchstone:needs-review"
    required_workflows: tuple[str, ...] = ()
    reap_after_hours: int = 6


@dataclass(frozen=True, slots=True)
class LoopConfig:
    name: str
    brief: str
    label: str
    config_dir: Path
    schedule: str | None = None
    protected_paths: tuple[str, ...] = ()
    require_change_under: tuple[str, ...] = ()
    confine_to: tuple[str, ...] = ()
    context: tuple[tuple[str, str], ...] = ()

    def prompt(self) -> str:
        return Template(self._brief_text(self.brief)).safe_substitute(dict(self.context))

    def review_prompt(self) -> str:
        if self.brief.startswith("builtin:"):
            return self._brief_text("builtin:review")
        return (self._brief_path(self.brief).parent / "review.md").read_text(encoding="utf-8")

    def _brief_text(self, reference: str) -> str:
        if reference.startswith("builtin:"):
            name = reference.removeprefix("builtin:")
            target = files("touchstone.resources").joinpath("briefs", f"{name}.md")
            if not target.is_file():
                raise ConfigError(f"unknown built-in brief {reference!r}")
            return target.read_text(encoding="utf-8")
        try:
            return self._brief_path(reference).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigError(f"brief does not exist: {self._brief_path(reference)}") from None

    def _brief_path(self, reference: str) -> Path:
        path = Path(reference).expanduser()
        return path if path.is_absolute() else (self.config_dir / path).resolve()


@dataclass(frozen=True, slots=True)
class Config:
    source: ConfigSource
    repo_path: Path
    state_dir: Path
    forge: ForgeConfig
    engine: EngineConfig
    execution: ExecutionConfig
    git: GitConfig
    loops: dict[str, LoopConfig]

    def loop(self, name: str) -> LoopConfig:
        try:
            return self.loops[name]
        except KeyError:
            known = ", ".join(sorted(self.loops)) or "none"
            raise ConfigError(f"no loop named {name!r}; configured loops are {known}") from None

    @property
    def execution_repo(self) -> str:
        if self.execution.target == "ssh" and self.execution.ssh is not None:
            return self.execution.ssh.workdir
        return str(self.repo_path)

    @property
    def execution_worktree(self) -> str:
        if self.execution.target == "ssh" and self.execution.ssh is not None:
            return str(PurePosixPath(self.execution.ssh.state_dir) / "worktree")
        return str(self.state_dir / "worktree")

    def describe(self) -> str:
        where = self.execution.target
        if self.execution.target == "ssh" and self.execution.ssh is not None:
            where = f"ssh {self.execution.ssh.host}"
        slug = self.forge.slug or "discovered repository"
        return (
            f"{slug} · engine={self.engine.name} model={self.engine.model} "
            f"effort={self.engine.audit_effort}/{self.engine.review_effort} · {where}"
        )


_TOP_LEVEL = {"version", "project", "state_dir", "forge", "engine", "execution", "git", "loop"}
_PROJECT = {"path"}
_FORGE = {
    "provider",
    "slug",
    "default_branch",
    "escalation_label",
    "required_workflows",
    "reap_after_hours",
}
_ENGINE = {
    "name",
    "model",
    "audit_effort",
    "review_effort",
    "timeout_seconds",
    "budget",
    "extra_args",
}
_BUDGET = {"audit", "review"}
_EXECUTION = {"target", "ssh"}
_SSH = {"host", "workdir", "state_dir", "env", "identity_file", "connect_timeout"}
_GIT = {"author_name", "author_email"}
_LOOP = {
    "brief",
    "label",
    "schedule",
    "protected_paths",
    "require_change_under",
    "confine_to",
    "context",
}


def _unknown(table: dict[str, Any], known: set[str], where: str) -> None:
    extra = sorted(set(table) - known)
    if extra:
        location = f"{where}." if where else ""
        raise ConfigError(f"unknown configuration key {location}{extra[0]}")


def _table(raw: dict[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        if required:
            raise ConfigError(f"configuration is missing the required table [{key}]")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _required(table: dict[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"[{where}] is missing the required key {key!r}")
    return table[key]


def _string(table: dict[str, Any], key: str, where: str, *, required: bool = False) -> None:
    if key not in table:
        if required:
            raise ConfigError(f"[{where}] is missing the required key {key!r}")
        return
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    if not value.strip():
        raise ConfigError(f"{where}.{key} must not be empty")


def _string_array(table: dict[str, Any], key: str, where: str) -> None:
    if key not in table:
        return
    value = table[key]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ConfigError(f"{where}.{key} must be an array of non-empty strings")


def _positive_int(table: dict[str, Any], key: str, where: str) -> None:
    if key not in table:
        return
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}.{key} must be a positive integer")


def _local_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _validate(raw: dict[str, Any]) -> None:
    _unknown(raw, _TOP_LEVEL, "")
    project = _table(raw, "project", required=True)
    forge = _table(raw, "forge", required=True)
    engine = _table(raw, "engine")
    execution = _table(raw, "execution")
    git = _table(raw, "git")
    loops = _table(raw, "loop")
    _unknown(project, _PROJECT, "project")
    _unknown(forge, _FORGE, "forge")
    _unknown(engine, _ENGINE, "engine")
    _unknown(_table(engine, "budget"), _BUDGET, "engine.budget")
    _unknown(execution, _EXECUTION, "execution")
    _unknown(_table(execution, "ssh"), _SSH, "execution.ssh")
    _unknown(git, _GIT, "git")
    _string(project, "path", "project", required=True)
    if "state_dir" in raw and not isinstance(raw["state_dir"], str):
        raise ConfigError("state_dir must be a string")
    for key in ("provider", "slug", "default_branch", "escalation_label"):
        _string(forge, key, "forge")
    _string_array(forge, "required_workflows", "forge")
    _positive_int(forge, "reap_after_hours", "forge")
    for key in ("name", "model", "audit_effort", "review_effort"):
        _string(engine, key, "engine")
    _positive_int(engine, "timeout_seconds", "engine")
    _string_array(engine, "extra_args", "engine")
    for key, value in _table(engine, "budget").items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ConfigError(f"engine.budget.{key} must be a non-negative number")
    _string(execution, "target", "execution")
    ssh = _table(execution, "ssh")
    for key in ("host", "workdir", "state_dir", "identity_file"):
        _string(ssh, key, "execution.ssh")
    _positive_int(ssh, "connect_timeout", "execution.ssh")
    ssh_env = ssh.get("env", {})
    if not isinstance(ssh_env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in ssh_env.items()
    ):
        raise ConfigError("execution.ssh.env must be a table of string values")
    for key in ("author_name", "author_email"):
        _string(git, key, "git")
    for name, value in loops.items():
        if not isinstance(value, dict):
            raise ConfigError(f"[loop.{name}] must be a table")
        _unknown(value, _LOOP, f"loop.{name}")
        context = value.get("context", {})
        if not isinstance(context, dict):
            raise ConfigError(f"[loop.{name}.context] must be a table")
        for key in ("brief", "label", "schedule"):
            _string(value, key, f"loop.{name}", required=key in {"brief", "label"})
        for key in ("protected_paths", "require_change_under", "confine_to"):
            _string_array(value, key, f"loop.{name}")
        if any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in context.items()
        ):
            raise ConfigError(f"loop.{name}.context must contain only string values")


def _state_dir(raw: dict[str, Any], base_dir: Path, *, identity: str) -> Path:
    override = os.environ.get("TOUCHSTONE_STATE")
    if override:
        return _local_path(override, Path.cwd())
    configured = raw.get("state_dir")
    if configured:
        return _local_path(str(configured), base_dir)
    xdg = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    leaf = re.sub(r"[^a-zA-Z0-9._-]+", "-", identity.rsplit("/", 1)[-1]).strip("-")
    leaf = leaf or "project"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
    return (xdg / "touchstone" / f"{leaf}-{digest}").expanduser().resolve()


def _loops(raw: dict[str, Any], base_dir: Path) -> dict[str, LoopConfig]:
    from touchstone.scheduling import ScheduleError, parse_schedule

    result: dict[str, LoopConfig] = {}
    for name, table in raw.items():
        schedule = table.get("schedule")
        if schedule is not None:
            try:
                parse_schedule(str(schedule))
            except ScheduleError as exc:
                raise ConfigError(f"loop.{name}.schedule: {exc}") from None
        result[name] = LoopConfig(
            name=name,
            brief=str(_required(table, "brief", f"loop.{name}")),
            label=str(_required(table, "label", f"loop.{name}")),
            config_dir=base_dir,
            schedule=str(schedule) if schedule is not None else None,
            protected_paths=tuple(table.get("protected_paths", ())),
            require_change_under=tuple(table.get("require_change_under", ())),
            confine_to=tuple(table.get("confine_to", ())),
            context=tuple(sorted(dict(table.get("context", {})).items())),
        )
    if not result:
        raise ConfigError("no [loop.*] sections; there is nothing to run")
    return result


def load_config(path: Path | None = None) -> Config:
    chosen = (path or discover_config_path()).expanduser().resolve()
    try:
        raw = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no configuration at {chosen}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{chosen} is not valid TOML: {exc}") from None

    version = raw.get("version")
    if version is None:
        raise ConfigError("unversioned configuration; run 'touchstone config migrate'")
    if version != 1:
        raise ConfigError(f"unsupported configuration version {version!r}; expected 1")
    _validate(raw)

    base_dir = chosen.parent
    project = _table(raw, "project", required=True)
    forge_raw = _table(raw, "forge", required=True)
    engine_raw = _table(raw, "engine")
    budget_raw = _table(engine_raw, "budget")
    execution_raw = _table(raw, "execution")
    ssh_raw = _table(execution_raw, "ssh")
    git_raw = _table(raw, "git")

    engine_name = os.environ.get("TOUCHSTONE_ENGINE", engine_raw.get("name", "codex"))
    if engine_name not in ("codex", "claude"):
        raise ConfigError(f"engine.name must be 'codex' or 'claude', not {engine_name!r}")
    engine = EngineConfig(
        name=engine_name,
        model=os.environ.get("TOUCHSTONE_MODEL", engine_raw.get("model", "")),
        audit_effort=os.environ.get("TOUCHSTONE_EFFORT", engine_raw.get("audit_effort", "high")),
        review_effort=os.environ.get(
            "TOUCHSTONE_REVIEW_EFFORT", engine_raw.get("review_effort", "high")
        ),
        timeout_seconds=int(
            os.environ.get("TOUCHSTONE_TIMEOUT", engine_raw.get("timeout_seconds", 2700))
        ),
        budget=Budget(
            audit=float(budget_raw.get("audit", 20.0)),
            review=float(budget_raw.get("review", 4.0)),
        ),
        extra_args=tuple(engine_raw.get("extra_args", ())),
    )

    ssh = None
    if ssh_raw:
        ssh = SshConfig(
            host=str(_required(ssh_raw, "host", "execution.ssh")),
            workdir=str(_required(ssh_raw, "workdir", "execution.ssh")),
            state_dir=str(_required(ssh_raw, "state_dir", "execution.ssh")),
            env=tuple(sorted(dict(ssh_raw.get("env", {})).items())),
            identity_file=ssh_raw.get("identity_file"),
            connect_timeout=int(ssh_raw.get("connect_timeout", 15)),
        )
    target = os.environ.get("TOUCHSTONE_TARGET", execution_raw.get("target", "local"))
    if target not in ("local", "ssh"):
        raise ConfigError(f"execution.target must be 'local' or 'ssh', not {target!r}")

    provider = forge_raw.get("provider", "github")
    if provider != "github":
        raise ConfigError(f"forge.provider must be 'github', not {provider!r}")

    repo_override = os.environ.get("TOUCHSTONE_REPO")
    repo_path = _local_path(
        repo_override or str(_required(project, "path", "project")),
        Path.cwd() if repo_override else base_dir,
    )
    return Config(
        source=ConfigSource(path=chosen, schema_version=1),
        repo_path=repo_path,
        state_dir=_state_dir(
            raw,
            base_dir,
            identity=str(forge_raw.get("slug") or repo_path),
        ),
        forge=ForgeConfig(
            slug=str(forge_raw.get("slug", "")),
            provider="github",
            default_branch=str(forge_raw.get("default_branch", "main")),
            escalation_label=str(forge_raw.get("escalation_label", "touchstone:needs-review")),
            required_workflows=tuple(forge_raw.get("required_workflows", ())),
            reap_after_hours=int(forge_raw.get("reap_after_hours", 6)),
        ),
        engine=engine,
        execution=ExecutionConfig(target=target, ssh=ssh),
        git=GitConfig(
            author_name=git_raw.get("author_name"), author_email=git_raw.get("author_email")
        ),
        loops=_loops(_table(raw, "loop"), base_dir),
    )


def discover_config_path(start: Path | None = None) -> Path:
    explicit = os.environ.get("TOUCHSTONE_CONFIG")
    if explicit:
        return Path(explicit)
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / "touchstone.toml"
        if candidate.exists():
            return candidate
        if (directory / ".git").exists():
            break
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    search = (
        xdg / "touchstone" / "config.toml",
        Path("/etc/touchstone/config.toml"),
    )
    for candidate in search:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(item) for item in search)
    raise ConfigError(f"no configuration found from {current}; also looked in {searched}")


def load(path: Path | None = None) -> Config:
    """Compatibility alias for the original public function."""
    return load_config(path)


__all__ = [
    "Budget",
    "Config",
    "ConfigError",
    "ConfigSource",
    "EngineConfig",
    "ExecutionConfig",
    "ForgeConfig",
    "GitConfig",
    "LoopConfig",
    "SshConfig",
    "discover_config_path",
    "load",
    "load_config",
]
