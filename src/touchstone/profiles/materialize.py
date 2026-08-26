"""Deterministic Profile materialization and generated-file refresh."""

from __future__ import annotations

import difflib
import hashlib
import json
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import tomli_w

from touchstone.config import Config, ConfigError, load
from touchstone.profiles.catalog import load_catalog
from touchstone.profiles.detect import detect_profiles
from touchstone.profiles.model import (
    MINIMAL_CAPABILITIES,
    Evidence,
    ProfileCatalog,
    ProfileMatch,
    TargetCandidate,
    ValidationCandidate,
)
from touchstone.profiles.targets import TargetDiscovery, discover_targets

_MANAGER_LOCKS = (
    ("npm", "node", ("package-lock.json", "npm-shrinkwrap.json")),
    ("pnpm", "node", ("pnpm-lock.yaml",)),
    ("yarn", "node", ("yarn.lock",)),
    ("bun", "node", ("bun.lock", "bun.lockb")),
    ("uv", "python", ("uv.lock",)),
    ("poetry", "python", ("poetry.lock",)),
    ("pdm", "python", ("pdm.lock",)),
)


@dataclass(frozen=True, slots=True)
class MaterializedConfig:
    data: dict[str, Any]
    text: str
    source_digest: str
    discovery: TargetDiscovery
    matches: dict[str, tuple[ProfileMatch, ...]]


@dataclass(frozen=True, slots=True)
class ProfileDiff:
    changed: bool
    diff: str
    current_text: str
    expected_text: str
    materialized: MaterializedConfig


@dataclass(frozen=True, slots=True)
class RefreshReport:
    path: Path
    changed: bool
    written: bool
    diff: str


def detect_repository(
    root: Path,
    *,
    explicit_profiles: tuple[str, ...] = (),
    target_ids_by_path: dict[Path, str] | None = None,
) -> tuple[TargetDiscovery, dict[str, tuple[ProfileMatch, ...]], ProfileCatalog]:
    repository = root.expanduser().resolve()
    catalog = load_catalog(repository / ".touchstone/profiles")
    for name in explicit_profiles:
        catalog.get(name)
    discovery = discover_targets(repository, target_ids_by_path=target_ids_by_path)
    detected_by_target: dict[str, list[ProfileMatch]] = {}
    for target in discovery.targets:
        detected_by_target[target.id] = list(
            detect_profiles(
                repository,
                TargetCandidate(id=target.id, path=target.path),
                catalog=catalog,
            )
        )
    selections: dict[str, list[str]] = {target.id: [] for target in discovery.targets}
    for name in explicit_profiles:
        matching = [
            target.id
            for target in discovery.targets
            if any(match.profile == name for match in detected_by_target[target.id])
        ]
        if not matching:
            if len(discovery.targets) != 1:
                raise ConfigError(
                    f"Profile {name!r} has no Target-specific evidence; "
                    "add it to one [target.NAME] table after init"
                )
            matching = [discovery.targets[0].id]
        for target_id in matching:
            selections[target_id].append(name)

    matches: dict[str, tuple[ProfileMatch, ...]] = {}
    for target in discovery.targets:
        detected = detected_by_target[target.id]
        detected = _apply_explicit_profiles(
            detected,
            tuple(selections[target.id]),
            target.path,
            catalog,
        )
        order = {name: index for index, name in enumerate(catalog.profiles)}
        matches[target.id] = tuple(
            sorted(detected, key=lambda match: order.get(match.profile, len(order)))
        )
    return discovery, matches, catalog


def _loop_names(loops: tuple[str, ...]) -> tuple[str, ...]:
    """Sorted and deduplicated, because the digest is taken over this table.

    A repository whose Loops arrived in a different order would otherwise
    regenerate to different bytes and read as drift on every check.
    """

    return tuple(sorted(dict.fromkeys(loops))) or ("code",)


def _naming_sentence(rules: dict[str, str]) -> str:
    """State declared naming rules as one line a brief can drop into a prompt.

    A session reads the rules; nothing else does, so they are rendered rather
    than passed as a table. Sorted so the same Profile Set always produces the
    same sentence and an unchanged repository produces an unchanged digest.
    """

    return "; ".join(
        f"{what.replace('_', ' ')} names are {convention}"
        for what, convention in sorted(rules.items())
    )


def materialize(
    discovery: TargetDiscovery,
    matches: dict[str, tuple[ProfileMatch, ...]],
    catalog: ProfileCatalog,
    *,
    repository: Path,
    package_managers: tuple[str, ...] = (),
    strict_package_managers: bool = False,
    loops: tuple[str, ...] = ("code",),
) -> MaterializedConfig:
    used_profiles: list[str] = []
    targets: dict[str, Any] = {}
    # `generic` is the base of every composition, not an alternative to a stack.
    # It is attached as a Match only when nothing else was detected, so anything
    # it contributes to a detected Target has to be read from the base directly.
    base = catalog.get("generic")
    protected = list(base.protected_paths)
    source_paths: list[str] = []
    context: list[str] = []
    naming: dict[str, str] = {}

    for target in discovery.targets:
        target_matches = matches.get(target.id, ())
        confirmed = [match.profile for match in target_matches if match.verdict == "confirmed"]
        for profile in confirmed:
            if profile not in used_profiles:
                used_profiles.append(profile)
        evidence = []
        target_protected: list[str] = []
        target_sources: list[str] = []
        audit_context: list[str] = []
        target_naming: dict[str, str] = {}
        target_managers = _target_package_managers(
            repository,
            target.path,
            confirmed,
            package_managers,
            strict=strict_package_managers,
        )
        # The base Gates first, so a Target with a detected stack still gets the
        # side-effect-minimal checks. Without this the only Gate any Profile
        # enables by default reached exactly the repositories Touchstone could
        # not identify, and `validate` was a no-op everywhere else.
        validations = [_gate_record(candidate, target_managers) for candidate in base.validation]
        for match in target_matches:
            definition = catalog.get(match.profile)
            for item in match.evidence:
                record: dict[str, Any] = {
                    "profile": match.profile,
                    "verdict": match.verdict,
                    "kind": item.kind,
                    "path": item.path,
                    "detail": item.detail,
                }
                if match.detected_version is not None:
                    record["detected_version"] = match.detected_version
                if match.warning:
                    record["warning"] = match.warning
                evidence.append(record)
            if match.verdict != "confirmed":
                continue
            _extend_unique(target_protected, definition.protected_paths)
            _extend_unique(target_sources, definition.source_paths)
            if definition.audit_context and definition.audit_context not in audit_context:
                audit_context.append(definition.audit_context)
            # A framework Profile composes on top of its language, so the more
            # specific one wins where both declare a rule for the same thing.
            target_naming.update(dict(definition.naming))
            for candidate in definition.validation:
                record = _gate_record(candidate, target_managers)
                if record not in validations:
                    validations.append(record)
        target_sources = _resolve_source_paths(repository, target.path, target_sources, confirmed)
        scoped_protected = [_scoped(target.path, value) for value in target_protected]
        scoped_sources = [_scoped_directory(target.path, value) for value in target_sources]
        _extend_unique(protected, scoped_protected)
        _extend_unique(source_paths, scoped_sources)
        label = ", ".join(confirmed) or "generic"
        context.append(f"{target.id} at {target.path.as_posix()} ({label})")
        naming.update(target_naming)
        targets[target.id] = {
            "path": target.path.as_posix(),
            "profiles": confirmed,
            "dependencies": list(target.dependencies),
            "package_managers": list(target_managers),
            "audit_context": audit_context,
            "naming": _naming_sentence(target_naming),
            "protected_paths": target_protected,
            "source_paths": target_sources,
            "evidence": evidence,
            "validation": validations,
        }

    profile_versions = {
        name: catalog.get(name).version
        for name in catalog.profiles
        if name in used_profiles or name == "generic"
    }
    loop_table: dict[str, Any] = {
        "protected_paths": protected,
        "require_change_under": source_paths,
        "context": {
            "project": "; ".join(context) or "this repository",
            "ledger": ("No project findings ledger is configured; treat the queue as empty."),
            "protected": "the generated and project-owned protected paths",
            "rules_clause": "",
        }
        # Absent rather than empty when no Profile declares a rule: an
        # empty value would override the brief's own default and hand
        # the session a blank where a sentence belongs.
        | ({"naming": _naming_sentence(naming)} if naming else {}),
    }
    data: dict[str, Any] = {
        "metadata": {
            "package_version": _package_version(),
            "package_managers": list(package_managers),
            "profile_versions": profile_versions,
        },
        "target": targets,
        # Every configured Loop, not one spelling of one. This table was written
        # for a Loop literally named `code`, so a project that named its Loops
        # anything else got no protected paths, no source confinement and no
        # rendered context at all — the brief fell back to "this repository" and
        # the classify checks to the built-in list. The only way to scope such a
        # Loop was to copy these values into the project file by hand, where
        # `profile refresh` could never update them again.
        "loop": {name: deepcopy(loop_table) for name in _loop_names(loops)},
    }
    digest = _digest(data)
    data["metadata"]["source_digest"] = digest
    text = (
        "# Generated by `touchstone profile refresh`; edit touchstone.toml instead.\n"
        "# Run `touchstone profile diff` to inspect regeneration.\n\n" + tomli_w.dumps(data)
    )
    return MaterializedConfig(data, text, digest, discovery, matches)


def profile_diff(config: Config) -> ProfileDiff:
    generated_path = config.source.generated_path
    if generated_path is None:
        raise ConfigError("Profile refresh requires a version 2 configuration")
    discovery, matches, catalog = detect_repository(
        config.repo_path,
        target_ids_by_path={target.path: target_id for target_id, target in config.targets.items()},
    )
    explicit_profiles = _explicit_profiles_by_target(config.source.path)
    order = {name: index for index, name in enumerate(catalog.profiles)}
    for target in discovery.targets:
        configured = config.targets.get(target.id)
        if configured is None:
            continue
        detected = _apply_explicit_profiles(
            list(matches[target.id]),
            explicit_profiles.get(target.id, ()),
            target.path,
            catalog,
        )
        matches[target.id] = tuple(
            sorted(detected, key=lambda match: order.get(match.profile, len(order)))
        )
    managers = (
        config.generated_metadata.package_managers if config.generated_metadata is not None else ()
    )
    generated = materialize(
        discovery,
        matches,
        catalog,
        repository=config.repo_path,
        package_managers=managers,
        loops=tuple(config.loops),
    )
    try:
        current = generated_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    changed = current != generated.text
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            generated.text.splitlines(keepends=True),
            fromfile=str(generated_path),
            tofile=f"{generated_path} (regenerated)",
        )
    )
    return ProfileDiff(changed, diff, current, generated.text, generated)


def _explicit_profiles_by_target(path: Path) -> dict[str, tuple[str, ...]]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    targets = raw.get("target", {})
    if not isinstance(targets, dict):
        return {}
    explicit: dict[str, tuple[str, ...]] = {}
    for target_id, value in targets.items():
        profiles = value.get("profiles", []) if isinstance(value, dict) else []
        if (
            isinstance(target_id, str)
            and isinstance(profiles, list)
            and all(isinstance(profile, str) for profile in profiles)
        ):
            explicit[target_id] = tuple(profiles)
    return explicit


def refresh_profiles(config_path: Path, *, write: bool) -> RefreshReport:
    config = load(config_path)
    report = profile_diff(config)
    path = config.source.generated_path
    if path is None:
        raise ConfigError("Profile refresh requires a version 2 configuration")
    written = False
    if write and report.changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.refreshing")
        try:
            temporary.write_text(report.expected_text, encoding="utf-8")
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        written = True
    return RefreshReport(path, report.changed, written, report.diff)


def detect_package_managers(root: Path) -> tuple[str, ...]:
    repository = root.expanduser().resolve()
    found: list[str] = []
    for manager, _ecosystem, files in _MANAGER_LOCKS:
        if any((repository / name).is_file() for name in files):
            found.append(manager)
    package = _read_json(repository / "package.json")
    declared = package.get("packageManager") if package is not None else None
    if isinstance(declared, str):
        manager = declared.split("@", 1)[0]
        if manager in {item[0] for item in _MANAGER_LOCKS} and manager not in found:
            found.append(manager)
    pyproject = _read_toml(repository / "pyproject.toml")
    tool = pyproject.get("tool", {}) if pyproject is not None else {}
    if isinstance(tool, dict):
        for manager in ("uv", "poetry", "pdm"):
            if manager in tool and manager not in found:
                found.append(manager)
    order = {manager: index for index, (manager, _, _) in enumerate(_MANAGER_LOCKS)}
    return tuple(sorted(found, key=lambda manager: order[manager]))


def ambiguous_package_managers(managers: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    ecosystems = {manager: ecosystem for manager, ecosystem, _ in _MANAGER_LOCKS}
    groups = []
    for ecosystem in ("node", "python"):
        group = tuple(manager for manager in managers if ecosystems[manager] == ecosystem)
        if len(group) > 1:
            groups.append(group)
    return tuple(groups)


def select_package_managers(
    root: Path,
    explicit: str | None = None,
    *,
    target_paths: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    found_list = list(detect_package_managers(root))
    for target in target_paths:
        for manager in detect_package_managers(root / target):
            if manager not in found_list:
                found_list.append(manager)
    order = {manager: index for index, (manager, _, _) in enumerate(_MANAGER_LOCKS)}
    found = tuple(sorted(found_list, key=lambda manager: order[manager]))
    ambiguous = ambiguous_package_managers(found)
    if not ambiguous:
        if explicit is not None and explicit not in found:
            raise ConfigError(f"package manager {explicit!r} has no repository evidence")
        return found
    if explicit is None:
        choices = ", ".join("/".join(group) for group in ambiguous)
        raise ConfigError(
            f"ambiguous package manager evidence ({choices}); pass --package-manager NAME"
        )
    if explicit not in found:
        raise ConfigError(f"package manager {explicit!r} has no repository evidence")
    ambiguous_names = {name for group in ambiguous for name in group}
    return tuple(name for name in found if name not in ambiguous_names or name == explicit)


def _apply_explicit_profiles(
    detected: list[ProfileMatch],
    explicit_profiles: tuple[str, ...],
    target: Path,
    catalog: ProfileCatalog,
) -> list[ProfileMatch]:
    for name in explicit_profiles:
        catalog.get(name)
        evidence = Evidence("explicit", target.as_posix(), "selected by project owner")
        existing = next((match for match in detected if match.profile == name), None)
        if existing is None:
            detected.append(ProfileMatch(name, "confirmed", (evidence,)))
            continue
        if existing.verdict != "confirmed":
            detected[detected.index(existing)] = ProfileMatch(
                name,
                "confirmed",
                (*existing.evidence, evidence),
                existing.detected_version,
                existing.warning,
            )
    return detected


_JAVASCRIPT_MANAGERS = ("npm", "pnpm", "yarn", "bun")


def _manager_argv(
    argv: tuple[str, ...],
    capability: str,
    managers: tuple[str, ...],
) -> tuple[str, ...]:
    """Express a Profile's JavaScript candidate in the Target's own package manager."""

    manager = next((name for name in managers if name in _JAVASCRIPT_MANAGERS), "")
    if not manager:
        return argv
    if capability == "package-script":
        script, rest = _script_invocation(argv)
        if script is None:
            return argv
        return (manager, "run", script, *rest)
    if capability == "package-executable":
        tool, rest = _executable_invocation(argv)
        if tool is None:
            return argv
        if manager == "npm":
            return ("npx", tool, *rest)
        if manager == "pnpm":
            return ("pnpm", "exec", tool, *rest)
        if manager == "bun":
            return ("bun", "x", tool, *rest)
        return ("yarn", "run", tool, *rest)
    return argv


def _script_invocation(argv: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    if len(argv) < 2 or argv[0] not in _JAVASCRIPT_MANAGERS:
        return None, ()
    if argv[1] == "run":
        return (argv[2], argv[3:]) if len(argv) >= 3 else (None, ())
    return argv[1], argv[2:]


def _executable_invocation(argv: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    if len(argv) < 2:
        return None, ()
    if argv[0] == "npx":
        return argv[1], argv[2:]
    if argv[0] == "pnpm" and argv[1] == "exec" and len(argv) >= 3:
        return argv[2], argv[3:]
    if argv[0] == "bun" and argv[1] == "x" and len(argv) >= 3:
        return argv[2], argv[3:]
    if argv[0] == "yarn" and argv[1] == "run" and len(argv) >= 3:
        return argv[2], argv[3:]
    return None, ()


def _target_package_managers(
    repository: Path,
    target: Path,
    profiles: list[str],
    repository_managers: tuple[str, ...],
    *,
    strict: bool,
) -> tuple[str, ...]:
    local = detect_package_managers(repository / target)
    if local:
        ambiguous = ambiguous_package_managers(local)
        if ambiguous:
            ambiguous_names = {name for group in ambiguous for name in group}
            selected = {name for name in repository_managers if name in ambiguous_names}
            unresolved = tuple(group for group in ambiguous if not selected.intersection(group))
            if unresolved and strict:
                choices = ", ".join("/".join(group) for group in unresolved)
                raise ConfigError(
                    f"ambiguous package manager evidence for Target {target.as_posix()} "
                    f"({choices}); select one package manager and rerun init"
                )
            if not unresolved:
                return tuple(
                    name for name in local if name not in ambiguous_names or name in selected
                )
        return local
    ecosystems: set[str] = set()
    if set(profiles) & {"javascript", "node", "typescript", "react", "nextjs"}:
        ecosystems.add("node")
    if set(profiles) & {"python", "fastapi", "django"}:
        ecosystems.add("python")
    manager_ecosystems = {manager: ecosystem for manager, ecosystem, _ in _MANAGER_LOCKS}
    return tuple(
        manager for manager in repository_managers if manager_ecosystems.get(manager) in ecosystems
    )


def _scoped(target: Path, value: str) -> str:
    if target == Path("."):
        return value
    return (target / value).as_posix()


def _scoped_directory(target: Path, value: str) -> str:
    """A source path scoped to its Target, always in directory form.

    `Path` drops the trailing separator, which turned `src/` into `apps/web/src`
    and left the consumer matching a bare prefix: `apps/web/apple.ts` counted as
    a change under `apps/web/app`.
    """

    scoped = _scoped(target, value).rstrip("/")
    return f"{scoped}/" if scoped else "./"


def _gate_record(candidate: ValidationCandidate, managers: tuple[str, ...]) -> dict[str, Any]:
    return {
        "argv": list(_manager_argv(candidate.argv, candidate.capability, managers)),
        "timeout_seconds": candidate.timeout_seconds,
        "capability": candidate.capability,
        # A Profile enables only side-effect-minimal Gates. Anything that runs
        # project code stays a disabled Candidate until the project override
        # accepts it.
        "enabled": candidate.capability in MINIMAL_CAPABILITIES,
    }


#: Directories that sit next to source but are not it.
_NOT_SOURCE = frozenset(
    {"__pycache__", "build", "dist", "node_modules", "site-packages", "venv", "env"}
)


def _resolve_source_paths(
    repository: Path,
    target: Path,
    declared: list[str],
    profiles: list[str],
) -> list[str]:
    """The source directories a Target actually has.

    A Profile declares the layouts it knows about, which is a guess until the
    repository is read. Materializing the guess verbatim produced a
    `require_change_under` that named three directories none of which existed
    for a flat-layout Python package, so the loop discarded every source-only
    change it made to that Target — after paying for the audit that found it.
    """

    root = repository / target
    resolved = [value for value in declared if (root / value).is_dir()]
    if "python" in profiles:
        _extend_unique(resolved, _python_package_directories(root))
    if resolved:
        return resolved
    # A Target whose declared layout is absent still owns its own directory.
    # The repository root is the exception: `.` is under every path, and
    # claiming it would neuter the gate for every other Target in the workspace.
    return [] if target == Path(".") else ["."]


def _python_package_directories(root: Path) -> list[str]:
    """Importable packages sitting directly in a Target, for flat layouts.

    A `src/` layout needs nothing from here — the Profile already names it, and
    it is kept because it exists. This finds the other convention, where the
    package is a sibling of `pyproject.toml` and its name is the project's own.
    """

    if not root.is_dir():
        return []
    found = []
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in _NOT_SOURCE:
            continue
        if (entry / "__init__.py").is_file():
            found.append(f"{entry.name}/")
    return found


def _extend_unique(values: list[str], additions: tuple[str, ...] | list[str]) -> None:
    for value in additions:
        if value not in values:
            values.append(value)


def _digest(data: dict[str, Any]) -> str:
    normalized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(normalized.encode()).hexdigest()}"


def _package_version() -> str:
    try:
        return version("touchstone-agent")
    except PackageNotFoundError:
        return "0+unknown"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


__all__ = [
    "MaterializedConfig",
    "ProfileDiff",
    "RefreshReport",
    "ambiguous_package_managers",
    "detect_package_managers",
    "detect_repository",
    "materialize",
    "profile_diff",
    "refresh_profiles",
    "select_package_managers",
]
