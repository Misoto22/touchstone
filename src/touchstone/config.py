"""Everything the loop can be pointed at, in one place.

The bash implementation this replaces hardcoded a repository path, a model
name, an effort level and the assumption that everything ran on the machine
holding the git clone. Each of those turned out to be wrong at least once:
the model changed, the effort had to be pinned rather than inherited, and the
laptop turned out to be the wrong host — it sleeps, and it cannot reach the
production database that tells a latent defect from a live one.

So none of them are decisions this code makes. It reads them, and says clearly
what it read, because a loop that merges to production unattended should never
leave anyone guessing which model just approved something.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

EngineName = Literal["codex", "claude"]
Target = Literal["local", "ssh"]

#: Where a config file is looked for when none is named. Later entries win, so
#: a checkout's own file overrides the one installed for the machine.
SEARCH = (
    Path("/etc/touchstone/config.toml"),
    Path.home() / ".config" / "touchstone" / "config.toml",
    Path("touchstone.toml"),
)


class ConfigError(ValueError):
    """The configuration is unusable, and the run should not start."""


@dataclass(frozen=True, slots=True)
class Budget:
    """Ceilings on one session, in dollars.

    Only Claude reports and enforces a cost. Codex does neither, so on that
    engine these are unenforceable and `Engine.reports_cost` says so rather
    than letting a number in a config file imply a guarantee that does not
    exist.
    """

    audit: float = 20.0
    review: float = 4.0


@dataclass(frozen=True, slots=True)
class EngineConfig:
    name: EngineName = "codex"
    model: str = "gpt-5.6-sol"
    #: Pinned rather than inherited. The default is undocumented on both
    #: engines and free to move, and effort drives both what a run costs and
    #: whether a finding is real.
    audit_effort: str = "high"
    review_effort: str = "high"
    #: A wedged session otherwise holds the lock for as long as its process
    #: lives, and a stale-lock check only breaks a lock whose pid is gone.
    timeout_seconds: int = 2700
    budget: Budget = field(default_factory=Budget)
    #: Passed through untouched. An engine gains flags faster than this file
    #: can learn about them.
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SshConfig:
    host: str
    #: Where the clone and the loop's state live on that host. Not derived from
    #: the local paths: the remote is a different machine with a different
    #: layout, and guessing is how a job ends up writing to a directory nobody
    #: is watching.
    workdir: str
    state_dir: str
    #: Prefixed to every remote command. `ssh` gives a non-login shell whose
    #: PATH has neither Homebrew nor ~/.local/bin, which is the same reason the
    #: launchd plists carry an explicit PATH.
    env: tuple[tuple[str, str], ...] = ()
    identity_file: str | None = None
    connect_timeout: int = 15


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    target: Target = "local"
    ssh: SshConfig | None = None

    def __post_init__(self) -> None:
        if self.target == "ssh" and self.ssh is None:
            raise ConfigError("execution.target is 'ssh' but no [execution.ssh] section was given")


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    """The repository being audited, and how its pull requests are labelled."""

    slug: str
    default_branch: str = "main"
    audit_label: str = "auto:audit"
    harness_label: str = "auto:harness"
    escalation_label: str = "blocked:needs-henry"
    #: A pull request that CI has turned red never merges, and without reaping
    #: it would hold the single slot forever and stop the loop dead.
    reap_after_hours: int = 6


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """One scheduled loop. Two exist today: the code audit and the harness review."""

    name: str
    brief: Path
    label: str
    #: Paths a finding may not touch. Enforced after the fact, on the diff,
    #: because a permission layer models tools rather than intent — and on
    #: Codex, whose sandbox grants the whole worktree, it is the only check
    #: there is.
    protected_paths: tuple[str, ...] = ()
    #: When set, a diff that changes nothing under these paths opens no pull
    #: request at all. The harness review needs this; the code audit does not.
    require_change_under: tuple[str, ...] = ()
    #: When set, a diff touching anything outside these paths is escalated.
    confine_to: tuple[str, ...] = ()
    #: Names the brief substitutes into itself. The shipped briefs carry the
    #: behaviour that was learned the hard way — say latent when it is latent,
    #: one defect at a time, a verdict is a field — and know nothing about any
    #: particular repository. What a project calls its ledger, its rules and
    #: its risk policy belongs here.
    context: tuple[tuple[str, str], ...] = ()

    def prompt(self) -> str:
        """The brief with this loop's names substituted in.

        `string.Template` rather than `str.format`: a brief is mostly prose and
        JSON examples, and `{` appears in it constantly. A formatter would
        either choke or silently eat a brace from an example the session is
        meant to copy.

        `safe_substitute`, so an unnamed placeholder survives as itself rather
        than aborting the run — a brief with one stale name still audits, and
        failing the whole iteration over a word would be the worse trade.
        """
        from string import Template

        return Template(self.brief.read_text(encoding="utf-8")).safe_substitute(dict(self.context))


@dataclass(frozen=True, slots=True)
class Config:
    repo_path: Path
    state_dir: Path
    forge: ForgeConfig
    engine: EngineConfig
    execution: ExecutionConfig
    loops: dict[str, LoopConfig]

    def loop(self, name: str) -> LoopConfig:
        try:
            return self.loops[name]
        except KeyError:
            known = ", ".join(sorted(self.loops)) or "none"
            raise ConfigError(f"no loop named {name!r}; configured loops are {known}") from None

    def describe(self) -> str:
        """One line for the log, so a run says what it is before it costs anything.

        Where it runs comes from the target, not from whether an `[execution.ssh]`
        section happens to exist. Read the other way round it reported `ssh
        my-server` for a run executing locally — and a loop that merges to
        production unattended misreporting which machine it is on is the one
        thing this line exists to prevent.
        """
        where = self.execution.target
        if self.execution.target == "ssh" and self.execution.ssh is not None:
            where = f"ssh {self.execution.ssh.host}"
        return (
            f"{self.forge.slug} · engine={self.engine.name} model={self.engine.model} "
            f"effort={self.engine.audit_effort}/{self.engine.review_effort} · {where}"
        )


def _require(table: dict[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"{where} is missing the required key {key!r}")
    return table[key]


def _loops(raw: dict[str, Any]) -> dict[str, LoopConfig]:
    loops: dict[str, LoopConfig] = {}
    for name, table in raw.items():
        if not isinstance(table, dict):
            raise ConfigError(f"[loop.{name}] must be a table")
        loops[name] = LoopConfig(
            name=name,
            brief=Path(_require(table, "brief", f"[loop.{name}]")),
            label=_require(table, "label", f"[loop.{name}]"),
            protected_paths=tuple(table.get("protected_paths", ())),
            require_change_under=tuple(table.get("require_change_under", ())),
            confine_to=tuple(table.get("confine_to", ())),
            context=tuple(sorted(dict(table.get("context", {})).items())),
        )
    if not loops:
        raise ConfigError("no [loop.*] sections; there is nothing to run")
    return loops


def load(path: Path | None = None) -> Config:
    """Read a configuration, letting the environment override the volatile parts.

    The environment wins over the file for exactly the values an operator
    changes while debugging — engine, model, effort, where it runs. Everything
    structural stays in the file, where it can be reviewed.
    """
    chosen = path or _discover()
    try:
        raw = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no configuration at {chosen}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{chosen} is not valid TOML: {exc}") from None

    engine_raw = raw.get("engine", {})
    budget_raw = engine_raw.get("budget", {})
    engine = EngineConfig(
        name=os.environ.get("TOUCHSTONE_ENGINE", engine_raw.get("name", "codex")),  # type: ignore[arg-type]
        model=os.environ.get("TOUCHSTONE_MODEL", engine_raw.get("model", "gpt-5.6-sol")),
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
    if engine.name not in ("codex", "claude"):
        raise ConfigError(f"engine.name must be 'codex' or 'claude', not {engine.name!r}")

    execution_raw = raw.get("execution", {})
    ssh_raw = execution_raw.get("ssh")
    ssh = None
    if ssh_raw is not None:
        ssh = SshConfig(
            host=_require(ssh_raw, "host", "[execution.ssh]"),
            workdir=_require(ssh_raw, "workdir", "[execution.ssh]"),
            state_dir=_require(ssh_raw, "state_dir", "[execution.ssh]"),
            env=tuple(sorted(dict(ssh_raw.get("env", {})).items())),
            identity_file=ssh_raw.get("identity_file"),
            connect_timeout=int(ssh_raw.get("connect_timeout", 15)),
        )
    execution = ExecutionConfig(
        target=os.environ.get("TOUCHSTONE_TARGET", execution_raw.get("target", "local")),  # type: ignore[arg-type]
        ssh=ssh,
    )
    if execution.target not in ("local", "ssh"):
        raise ConfigError(f"execution.target must be 'local' or 'ssh', not {execution.target!r}")

    forge_raw = _require(raw, "forge", "the configuration")
    forge = ForgeConfig(
        slug=_require(forge_raw, "slug", "[forge]"),
        default_branch=forge_raw.get("default_branch", "main"),
        audit_label=forge_raw.get("audit_label", "auto:audit"),
        harness_label=forge_raw.get("harness_label", "auto:harness"),
        escalation_label=forge_raw.get("escalation_label", "blocked:needs-henry"),
        reap_after_hours=int(forge_raw.get("reap_after_hours", 6)),
    )

    return Config(
        repo_path=Path(
            os.environ.get("TOUCHSTONE_REPO", _require(raw, "repo_path", "the configuration"))
        ),
        state_dir=Path(
            os.environ.get("TOUCHSTONE_STATE", raw.get("state_dir", "~/.local/state/touchstone"))
        ).expanduser(),
        forge=forge,
        engine=engine,
        execution=execution,
        loops=_loops(raw.get("loop", {})),
    )


def _discover() -> Path:
    for candidate in reversed(SEARCH):
        if candidate.exists():
            return candidate
    searched = ", ".join(str(p) for p in SEARCH)
    raise ConfigError(f"no configuration found; looked in {searched}")


__all__ = [
    "Budget",
    "Config",
    "ConfigError",
    "EngineConfig",
    "ExecutionConfig",
    "ForgeConfig",
    "LoopConfig",
    "SshConfig",
    "load",
]
