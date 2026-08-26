"""One project's configuration, fanned out to the repositories it covers.

A project holds decisions that are the same everywhere — which Loops run, on
which engines, on what schedule — and the repositories it covers hold the
evidence that is different everywhere. Keeping the first in one file and
rendering it into the second is what lets a fleet share a Loop without any
repository losing its own Validation Gate authorization, its own state, or its
own credential domain.

Nothing here writes to a repository. `sync_check` reads; proposing a rendered
file is a pull request like any other change, because the configuration file is
the root of Touchstone's permission model and a mechanism that could edit it
unattended could grant itself a Gate.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from touchstone.config_v2 import merge_generated


class FleetError(ValueError):
    """The project configuration is unusable, and no member may be rendered."""


#: Keys the fleet may never own, and why.
#:
#: `target` carries Validation Gate authorization — which command a repository
#: has agreed may run its own code. Handing that to whoever writes the central
#: file moves the decision away from the repository it endangers. `generated`
#: names the machine-owned file, whose location is the repository's business.
#: `project` and `state_dir` are paths on the machine running the Loop.
_FLEET_FORBIDDEN = {
    "target": "Validation Gate authorization stays in the repository being audited",
    "generated": "the machine-owned file's location is the repository's own",
    "project": "a repository-local path cannot be decided centrally",
    "state_dir": "a repository-local path cannot be decided centrally",
    "version": "the rendered file is a fragment, not a configuration in itself",
}

_MEMBER_KEYS = {"path", "loops", "overrides"}


@dataclass(frozen=True, slots=True)
class Member:
    slug: str
    path: Path
    loops: tuple[str, ...]
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    path: Path
    name: str
    defaults: dict[str, Any]
    members: dict[str, Member]


@dataclass(frozen=True, slots=True)
class SyncReport:
    """Which members disagree with what the project would render for them."""

    drifted: tuple[str, ...]
    missing: tuple[str, ...]
    matched: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 3 if self.drifted else 0


def load_project(path: Path) -> ProjectConfig:
    chosen = path.expanduser().resolve()
    try:
        raw = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FleetError(f"no project configuration at {chosen}") from None
    except tomllib.TOMLDecodeError as exc:
        raise FleetError(f"{chosen} is not valid TOML: {exc}") from None

    if raw.get("version") != 1:
        raise FleetError("project configuration requires version = 1")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FleetError("project configuration requires a non-empty name")
    unknown = sorted(set(raw) - {"version", "name", "defaults", "member"})
    if unknown:
        raise FleetError(f"unknown project key {unknown[0]}")

    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise FleetError("[defaults] must be a table")
    _refuse_forbidden(defaults, "defaults")
    _refuse_credential_values(defaults, "defaults")

    declared = set(defaults.get("loop", {}))
    members: dict[str, Member] = {}
    raw_members = raw.get("member", {})
    if not isinstance(raw_members, dict):
        raise FleetError('[member."<slug>"] must be a table keyed by repository slug')
    for slug, value in raw_members.items():
        if not isinstance(value, dict):
            raise FleetError(f"member {slug!r} must be a table")
        extra = sorted(set(value) - _MEMBER_KEYS)
        if extra:
            raise FleetError(f"unknown key member.{slug!r}.{extra[0]}")
        if "/" not in slug:
            raise FleetError(f"member {slug!r} must be an owner/repository slug")
        member_path = value.get("path")
        if not isinstance(member_path, str) or not member_path.strip():
            raise FleetError(f"member {slug!r} requires a path to its checkout")
        loops = value.get("loops", sorted(declared))
        if not isinstance(loops, list) or any(not isinstance(item, str) for item in loops):
            raise FleetError(f"member.{slug!r}.loops must be a list of Loop names")
        absent = [name for name in loops if name not in declared]
        if absent:
            known = ", ".join(sorted(declared)) or "none"
            raise FleetError(
                f"member {slug!r} takes Loop {absent[0]!r}, which the project does not "
                f"declare; declared Loops are {known}"
            )
        overrides = value.get("overrides", {})
        if not isinstance(overrides, dict):
            raise FleetError(f"member.{slug!r}.overrides must be a table")
        _refuse_forbidden(overrides, f"member.{slug!r}.overrides")
        _refuse_credential_values(overrides, f"member.{slug!r}.overrides")
        members[slug] = Member(
            slug=slug,
            path=(chosen.parent / member_path).resolve(),
            loops=tuple(loops),
            overrides=overrides,
        )
    if not members:
        raise FleetError("a project covers at least one member repository")
    return ProjectConfig(path=chosen, name=name, defaults=defaults, members=members)


def render(project: ProjectConfig, slug: str) -> str:
    """The fleet-owned fragment this member's configuration extends.

    Deterministic: the same project renders the same bytes, so `sync --check`
    compares meaning rather than formatting and an unchanged project never
    proposes a change.
    """

    member = project.members.get(slug)
    if member is None:
        known = ", ".join(sorted(project.members))
        raise FleetError(f"{slug!r} is not a member of this project; members are {known}")

    data = merge_generated(_without_loops(project.defaults), member.overrides)
    loops = project.defaults.get("loop", {})
    data["loop"] = {name: loops[name] for name in sorted(member.loops)}
    data = merge_generated(data, {"loop": member.overrides.get("loop", {})})
    forge = dict(data.get("forge", {}))
    forge["slug"] = slug
    data["forge"] = forge

    header = (
        f"# Generated by `touchstone sync` from the {project.name!r} project.\n"
        "# Fleet-owned: edit the project file, not this one. Keys this repository\n"
        "# disagrees with belong in its own touchstone.toml, which wins.\n\n"
    )
    return header + tomli_w.dumps(_sorted(data))


def sync_check(project: ProjectConfig) -> SyncReport:
    """Compare every member's committed fragment with what the project renders.

    Read-only, and deliberately without a write counterpart: a rendered
    fragment reaches a repository as a proposal that a person merges.
    """

    drifted: list[str] = []
    missing: list[str] = []
    matched: list[str] = []
    for slug, member in project.members.items():
        expected = render(project, slug)
        target = member.path / ".touchstone" / "fleet.toml"
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            missing.append(slug)
            drifted.append(slug)
            continue
        if current == expected:
            matched.append(slug)
        else:
            drifted.append(slug)
    return SyncReport(tuple(drifted), tuple(missing), tuple(matched))


@dataclass(frozen=True, slots=True)
class ProposeReport:
    """What `sync --pr` did for each member, in the member's own words."""

    proposed: tuple[tuple[str, str], ...] = ()
    unchanged: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def sync_propose(project: ProjectConfig, executor: Any, *, branch_prefix: str) -> ProposeReport:
    """Open one pull request per member whose fragment has drifted.

    Deliberately not a direct write. The configuration file is the root of
    Touchstone's permission model — a mechanism able to edit it unattended
    could enable a Validation Gate for itself — so a fleet change arrives the
    same way every other change does, and a person merges it.
    """

    proposed: list[tuple[str, str]] = []
    unchanged: list[str] = []
    failed: list[tuple[str, str]] = []
    for slug, member in project.members.items():
        expected = render(project, slug)
        target = member.path / ".touchstone" / "fleet.toml"
        if target.exists() and target.read_text(encoding="utf-8") == expected:
            unchanged.append(slug)
            continue
        if not (member.path / ".git").exists():
            failed.append((slug, f"{member.path} is not a git checkout"))
            continue
        branch = f"{branch_prefix}{_digest(expected)}"
        error = _propose_one(executor, member, target, expected, branch, project.name)
        if error:
            failed.append((slug, error))
        else:
            proposed.append((slug, branch))
    return ProposeReport(tuple(proposed), tuple(unchanged), tuple(failed))


def _propose_one(
    executor: Any,
    member: Member,
    target: Path,
    expected: str,
    branch: str,
    project_name: str,
) -> str:
    where = str(member.path)
    steps: list[tuple[list[str], str]] = [
        (["git", "-C", where, "checkout", "-B", branch], "could not create the branch"),
    ]
    for argv, failure in steps:
        result = executor.run(argv, timeout=120)
        if not result.ok:
            return f"{failure}: {result.tail()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8")
    relative = target.relative_to(member.path).as_posix()
    subject = f"chore: sync touchstone configuration from {project_name}"
    after: list[tuple[list[str], str]] = [
        (["git", "-C", where, "add", relative], "could not stage the fragment"),
        (
            ["git", "-C", where, "commit", "--quiet", "-m", subject],
            "could not commit the fragment",
        ),
        (["git", "-C", where, "push", "--set-upstream", "origin", branch], "could not push"),
        (
            [
                "gh",
                "pr",
                "create",
                "--repo",
                member.slug,
                "--head",
                branch,
                "--title",
                subject,
                "--body",
                (
                    f"Rendered by `touchstone sync` from the `{project_name}` project.\n\n"
                    "This file is fleet-owned. Keys this repository disagrees with belong "
                    "in its own `touchstone.toml`, which wins over anything here."
                ),
            ],
            "could not open the pull request",
        ),
    ]
    for argv, failure in after:
        result = executor.run(argv, timeout=180)
        if not result.ok:
            return f"{failure}: {result.tail()}"
    return ""


def _digest(rendered: str) -> str:
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]


def render_compose(
    project: ProjectConfig,
    *,
    image: str = "ghcr.io/misoto22/touchstone",
    base: Path | None = None,
) -> str:
    """One container per member repository, each seeing only its own.

    A single container iterating every member would be the central-execution
    shape wearing a different name: one process holding every repository's
    credential, and a namespace inside it standing in for the container
    boundary that already exists. Separate services cost a few more containers
    and keep each blast radius the size of one repository.

    Credentials arrive as environment variables the operator injects. This file
    names them and never carries a value or an `op://` reference, because a
    Compose file is committed and a reference in it tells a reader which vault
    item to go after.
    """

    anchor = (base or project.path.parent).resolve()
    services: list[str] = []
    volumes: list[str] = []
    for slug in sorted(project.members):
        member = project.members[slug]
        service = slug.replace("/", "-").replace("_", "-").lower()
        volume = f"touchstone-state-{service}"
        volumes.append(f"  {volume}:")
        required = "\n".join(f"      #   {name}" for name in _member_key_variables(project, member))
        services.append(
            "\n".join(
                [
                    f"  {service}:",
                    f"    image: {image}:${{TOUCHSTONE_VERSION:-latest}}",
                    f"    container_name: touchstone-{service}",
                    "    restart: unless-stopped",
                    "    environment:",
                    "      # Injected by the operator; never written into this file.",
                    "      TOUCHSTONE_CONTAINER_INTERVAL_SECONDS: "
                    "${TOUCHSTONE_CONTAINER_INTERVAL_SECONDS:-900}",
                    "    env_file:",
                    "      # This file is not committed. It has to hold, at least:",
                    required,
                    f"      - ./secrets/{service}.env",
                    "    volumes:",
                    f"      - {_compose_path(member.path, anchor)}:/repository",
                    f"      - {volume}:/state",
                    "",
                ]
            )
        )
    header = (
        f"# Generated by `touchstone sync` from the {project.name!r} project.\n"
        "# One service per member repository: one checkout, one state volume,\n"
        "# one credential set. Secrets come from ./secrets/<service>.env, which\n"
        "# is not committed and which the operator fills from their own store.\n\n"
        "services:\n"
    )
    return header + "\n".join(services) + "\nvolumes:\n" + "\n".join(sorted(volumes)) + "\n"


def _member_key_variables(project: ProjectConfig, member: Member) -> tuple[str, ...]:
    """Which environment variables this member's own containers need filled.

    Named here because the Compose file is the only thing the operator reads
    when writing the env file, and a variable they never learn about surfaces
    as a model call failing inside a container hours later. Only the engines
    this member's Loops actually reach are listed; a member taking no Loop on
    the cheap engine has no business holding its key.

    Variable names only. A Compose file is committed, and an `op://` reference
    in one tells a reader which vault item to go after.
    """

    from touchstone.config import VENDOR_KEY_ENV

    engines = merge_generated(
        {"engine": project.defaults.get("engine", {})},
        {"engine": member.overrides.get("engine", {})},
    )["engine"]
    loops = merge_generated(
        {"loop": project.defaults.get("loop", {})},
        {"loop": member.overrides.get("loop", {})},
    )["loop"]

    def key_of(table: dict[str, Any]) -> str:
        declared = str(table.get("api_key_env", ""))
        if declared:
            return declared
        return VENDOR_KEY_ENV.get(str(table.get("name", "codex")), "ANTHROPIC_API_KEY")

    unnamed = {key: value for key, value in engines.items() if not isinstance(value, dict)}
    needed = set()
    for name in member.loops:
        chosen = str(loops.get(name, {}).get("engine", ""))
        member_engine = engines.get(chosen) if chosen else None
        needed.add(key_of(member_engine if isinstance(member_engine, dict) else unnamed))
    # The forge token is not an engine's, and publication cannot happen without
    # it, so it belongs in the same list rather than in prose somewhere else.
    needed.add("GH_TOKEN")
    return tuple(sorted(needed))


def _compose_path(path: Path, anchor: Path) -> str:
    """A path Compose can resolve from wherever the file was written.

    Compose reads a relative volume source against the Compose file's own
    directory, so an absolute one pins the fleet to the machine that rendered
    it. Falls back to absolute where no relative path exists — a checkout on
    another drive, for instance — because a wrong relative path is worse than
    an honest absolute one.
    """

    try:
        relative = Path(os.path.relpath(path, anchor))
    except ValueError:
        return path.as_posix()
    return relative.as_posix()


def _without_loops(defaults: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in defaults.items() if key != "loop"}


def _refuse_forbidden(table: dict[str, Any], where: str) -> None:
    for key, reason in _FLEET_FORBIDDEN.items():
        if key in table:
            raise FleetError(f"{where}.{key} may not be set by a project: {reason}")


def _refuse_credential_values(table: dict[str, Any], where: str) -> None:
    """Refuse anything in a credential-shaped key that is not a reference.

    A project file is read by whoever can read the fleet repository, which is a
    wider audience than any single member repository. A value pasted here would
    be rendered into every member it reaches.
    """

    for key, value in table.items():
        if isinstance(value, dict):
            _refuse_credential_values(value, f"{where}.{key}")
            continue
        if key == "api_key_ref" and not str(value).startswith("op://"):
            raise FleetError(
                f"{where}.api_key_ref must be an op:// reference; a credential value in a "
                "project file reaches every member it renders to"
            )


def _sorted(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted(value[key]) for key in sorted(value)}
    return value


__all__ = [
    "FleetError",
    "Member",
    "ProjectConfig",
    "ProposeReport",
    "SyncReport",
    "load_project",
    "render",
    "sync_check",
    "sync_propose",
]
