"""Bounded Project Target and workspace dependency discovery."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from touchstone.profiles.model import TargetCandidate

_MANIFESTS = ("package.json", "pyproject.toml")
_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".touchstone",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "examples",
    "fixtures",
    "node_modules",
    "site-packages",
    "vendor",
    "venv",
}


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    id: str
    path: Path
    dependencies: tuple[str, ...] = ()
    package_name: str = ""


@dataclass(frozen=True, slots=True)
class TargetDiscovery:
    targets: tuple[ProjectTarget, ...]
    candidates: tuple[TargetCandidate, ...]
    excluded: tuple[Path, ...]
    warnings: tuple[str, ...]
    workspace_container: bool = False


def discover_targets(root: Path) -> TargetDiscovery:
    repository = root.expanduser().resolve()
    if not repository.is_dir():
        raise ValueError(f"repository does not exist: {repository}")

    includes, excludes, rule_warnings = _workspace_rules(repository)
    submodules = _submodule_paths(repository)
    manifest_paths, excluded = _manifest_directories(repository, excludes, submodules)
    warnings = list(rule_warnings)
    member_paths: list[Path] = []

    for pattern in includes:
        if not _safe_pattern(pattern):
            warnings.append(f"Ignored workspace pattern outside repository: {pattern}")
            continue
        for relative in manifest_paths:
            if relative == Path(".") or not PurePosixPath(relative.as_posix()).match(pattern):
                continue
            if relative not in member_paths:
                member_paths.append(relative)

    workspace_declared = bool(includes)
    root_is_target = not workspace_declared or _root_has_independent_contract(repository)
    target_paths = ([Path(".")] if root_is_target else []) + member_paths
    targets, id_warnings = _build_targets(repository, target_paths)
    warnings.extend(id_warnings)

    owned = {target.path for target in targets}
    candidates: list[TargetCandidate] = []
    seen_candidates: set[Path] = set()
    for relative in manifest_paths:
        if relative == Path("."):
            continue
        if relative in owned or relative in seen_candidates:
            continue
        seen_candidates.add(relative)
        candidates.append(TargetCandidate(_suggested_id(relative, repository.name), relative))

    targets = _attach_dependencies(repository, targets)
    return TargetDiscovery(
        targets=targets,
        candidates=tuple(candidates),
        excluded=tuple(sorted(excluded, key=lambda item: item.as_posix())),
        warnings=tuple(warnings),
        workspace_container=workspace_declared and not root_is_target,
    )


def affected_targets(changed_paths: Iterable[str], discovery: TargetDiscovery) -> tuple[str, ...]:
    direct: list[str] = []
    deepest = sorted(
        discovery.targets,
        key=lambda target: (-len(target.path.parts), target.id),
    )
    for raw_path in changed_paths:
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            continue
        owner = next((target for target in deepest if _owns(target.path, path)), None)
        if owner is not None and owner.id not in direct:
            direct.append(owner.id)

    reverse: dict[str, set[str]] = {target.id: set() for target in discovery.targets}
    for target in discovery.targets:
        for dependency in target.dependencies:
            reverse.setdefault(dependency, set()).add(target.id)

    result = list(direct)
    pending = list(direct)
    order = {target.id: index for index, target in enumerate(discovery.targets)}
    while pending:
        current = pending.pop(0)
        for dependent in sorted(reverse.get(current, ()), key=lambda item: order[item]):
            if dependent not in result:
                result.append(dependent)
                pending.append(dependent)
    return tuple(result)


def _workspace_rules(root: Path) -> tuple[list[str], list[str], list[str]]:
    includes: list[str] = []
    excludes: list[str] = []
    warnings: list[str] = []

    package = _json(root / "package.json")
    if package is not None:
        workspaces = package.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        if isinstance(workspaces, list):
            _extend_rules(workspaces, includes, excludes)

    pnpm_path = root / "pnpm-workspace.yaml"
    if pnpm_path.is_file():
        try:
            _extend_rules(_pnpm_packages(pnpm_path.read_text(encoding="utf-8")), includes, excludes)
        except UnicodeDecodeError:
            warnings.append("Ignored unreadable pnpm-workspace.yaml")

    pyproject = _toml(root / "pyproject.toml")
    if pyproject is not None:
        tool = pyproject.get("tool", {})
        if isinstance(tool, dict):
            uv = tool.get("uv", {})
            if isinstance(uv, dict):
                workspace = uv.get("workspace", {})
                if isinstance(workspace, dict):
                    _extend_rules(workspace.get("members", []), includes, excludes)
                    _extend_excludes(workspace.get("exclude", []), excludes)
            pdm = tool.get("pdm", {})
            if isinstance(pdm, dict):
                workspace = pdm.get("workspace", {})
                if isinstance(workspace, dict):
                    _extend_rules(workspace.get("members", []), includes, excludes)
                    _extend_rules(workspace.get("includes", []), includes, excludes)
                    _extend_excludes(workspace.get("exclude", []), excludes)
                    _extend_excludes(workspace.get("excludes", []), excludes)
    return includes, excludes, warnings


def _extend_rules(values: object, includes: list[str], excludes: list[str]) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip().replace("\\", "/")
        destination = excludes if normalized.startswith("!") else includes
        pattern = normalized.removeprefix("!")
        if pattern not in destination:
            destination.append(pattern)


def _extend_excludes(values: object, excludes: list[str]) -> None:
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value.strip():
                normalized = value.strip().removeprefix("!").replace("\\", "/")
                if normalized not in excludes:
                    excludes.append(normalized)


def _pnpm_packages(text: str) -> list[str]:
    values: list[str] = []
    in_packages = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith((" ", "\t")):
            in_packages = stripped.startswith("packages:")
            inline = stripped.removeprefix("packages:").strip() if in_packages else ""
            if inline.startswith("[") and inline.endswith("]"):
                values.extend(
                    part.strip().strip("'\"") for part in inline[1:-1].split(",") if part.strip()
                )
            continue
        if in_packages and stripped.startswith("-"):
            value = stripped[1:].strip().strip("'\"")
            if value:
                values.append(value)
    return values


def _safe_pattern(pattern: str) -> bool:
    path = PurePosixPath(pattern)
    return bool(pattern) and not path.is_absolute() and ".." not in path.parts


def _manifest_directories(
    root: Path, patterns: list[str], submodules: tuple[Path, ...]
) -> tuple[list[Path], set[Path]]:
    """Walk once while pruning trees that detection is not allowed to follow."""
    found: list[Path] = []
    excluded: set[Path] = set()
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(root)
        relative = relative if relative.parts else Path(".")
        if any(name in filenames for name in _MANIFESTS):
            found.append(relative)
        allowed = []
        for name in sorted(directories):
            child = current_path / name
            child_relative = child.relative_to(root)
            if child.is_symlink() or _is_excluded(child_relative, patterns, submodules):
                excluded.add(child_relative)
            else:
                allowed.append(name)
        directories[:] = allowed
    return sorted(found, key=lambda item: item.as_posix()), excluded


def _submodule_paths(root: Path) -> tuple[Path, ...]:
    path = root / ".gitmodules"
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ()
    values = []
    for match in re.finditer(r"^\s*path\s*=\s*(.+?)\s*$", text, re.MULTILINE):
        value = Path(match.group(1).strip())
        if not value.is_absolute() and ".." not in value.parts:
            values.append(value)
    return tuple(values)


def _is_excluded(path: Path, patterns: list[str], submodules: tuple[Path, ...]) -> bool:
    if any(part in _EXCLUDED_PARTS for part in path.parts):
        return True
    pure = PurePosixPath(path.as_posix())
    if any(pure.match(pattern) for pattern in patterns if _safe_pattern(pattern)):
        return True
    return any(path == submodule or path.is_relative_to(submodule) for submodule in submodules)


def _root_has_independent_contract(root: Path) -> bool:
    if any((root / name).exists() for name in ("src", "app", "lib", "manage.py")):
        return True
    package = _json(root / "package.json")
    if package is not None and isinstance(package.get("scripts"), dict) and package["scripts"]:
        return True
    pyproject = _toml(root / "pyproject.toml")
    return pyproject is not None and isinstance(pyproject.get("project"), dict)


def _build_targets(root: Path, paths: list[Path]) -> tuple[tuple[ProjectTarget, ...], list[str]]:
    targets: list[ProjectTarget] = []
    counts: dict[str, int] = {}
    warnings: list[str] = []
    for path in paths:
        base = _suggested_id(path, root.name)
        counts[base] = counts.get(base, 0) + 1
        target_id = base if counts[base] == 1 else f"{base}-{counts[base]}"
        if target_id != base:
            warnings.append(
                f"Target ID {base!r} was disambiguated as {target_id!r} for {path.as_posix()}"
            )
        targets.append(
            ProjectTarget(
                id=target_id,
                path=path,
                package_name=_package_name(root / path),
            )
        )
    return tuple(targets), warnings


def _suggested_id(path: Path, root_name: str) -> str:
    value = root_name if path == Path(".") else path.name
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "target"


def _package_name(path: Path) -> str:
    package = _json(path / "package.json")
    if package is not None and isinstance(package.get("name"), str):
        return str(package["name"])
    pyproject = _toml(path / "pyproject.toml")
    if pyproject is not None:
        project = pyproject.get("project")
        if isinstance(project, dict) and isinstance(project.get("name"), str):
            return canonicalize_name(str(project["name"]))
        tool = pyproject.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, dict) else None
        if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
            return canonicalize_name(str(poetry["name"]))
    return ""


def _attach_dependencies(
    root: Path, targets: tuple[ProjectTarget, ...]
) -> tuple[ProjectTarget, ...]:
    node_names = {target.package_name: target.id for target in targets if target.package_name}
    python_names = {
        canonicalize_name(target.package_name): target.id
        for target in targets
        if target.package_name
    }
    order = {target.id: index for index, target in enumerate(targets)}
    result = []
    for target in targets:
        names = _declared_dependencies(root / target.path)
        dependency_ids = {
            dependency
            for name in names
            for dependency in (
                node_names.get(name),
                python_names.get(canonicalize_name(name)),
            )
            if dependency is not None and dependency != target.id
        }
        result.append(
            replace(
                target,
                dependencies=tuple(sorted(dependency_ids, key=lambda item: order[item])),
            )
        )
    return tuple(result)


def _declared_dependencies(path: Path) -> set[str]:
    dependencies: set[str] = set()
    package = _json(path / "package.json")
    if package is not None:
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            table = package.get(key)
            if isinstance(table, dict):
                dependencies.update(name for name in table if isinstance(name, str))
    pyproject = _toml(path / "pyproject.toml")
    if pyproject is not None:
        project = pyproject.get("project")
        if isinstance(project, dict):
            for value in project.get("dependencies", []):
                if isinstance(value, str):
                    try:
                        dependencies.add(canonicalize_name(Requirement(value).name))
                    except InvalidRequirement:
                        continue
        tool = pyproject.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, dict) else None
        table = poetry.get("dependencies") if isinstance(poetry, dict) else None
        if isinstance(table, dict):
            dependencies.update(
                canonicalize_name(name)
                for name in table
                if isinstance(name, str) and name != "python"
            )
    return dependencies


def _json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _owns(target: Path, changed: PurePosixPath) -> bool:
    if target == Path("."):
        return True
    pure_target = PurePosixPath(target.as_posix())
    return changed == pure_target or pure_target in changed.parents


__all__ = [
    "ProjectTarget",
    "TargetDiscovery",
    "affected_targets",
    "discover_targets",
]
