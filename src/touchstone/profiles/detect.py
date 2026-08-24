"""Bounded, data-only detection for the built-in Profile catalog."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from touchstone.profiles.catalog import load_catalog
from touchstone.profiles.model import Evidence, ProfileMatch, TargetCandidate


def detect_profiles(root: Path, target: TargetCandidate) -> tuple[ProfileMatch, ...]:
    repository = root.expanduser().resolve()
    target_root = (repository / target.path).resolve()
    if not target_root.is_relative_to(repository):
        raise ValueError("Target path must stay inside the repository")
    catalog = load_catalog()
    matches: list[ProfileMatch] = []

    package_path = target_root / "package.json"
    package = _json_table(package_path)
    node_dependencies: dict[str, str] = {}
    if package is not None:
        node_dependencies = _node_dependencies(package)
        matches.append(_match("javascript", _evidence(repository, package_path, "manifest")))
        engines = package.get("engines", {})
        if isinstance(engines, dict) and isinstance(engines.get("node"), str):
            matches.append(
                _versioned_match(
                    "node",
                    str(engines["node"]),
                    _evidence(repository, package_path, "engines.node"),
                    catalog.get("node").supported,
                )
            )
        if "typescript" in node_dependencies or any(target_root.glob("tsconfig*.json")):
            source = (
                package_path
                if "typescript" in node_dependencies
                else next(target_root.glob("tsconfig*.json"))
            )
            matches.append(
                _versioned_match(
                    "typescript",
                    node_dependencies.get("typescript", ""),
                    _evidence(repository, source, "TypeScript configuration"),
                    catalog.get("typescript").supported,
                )
            )
        if "react" in node_dependencies:
            matches.append(
                _versioned_match(
                    "react",
                    node_dependencies["react"],
                    _evidence(repository, package_path, "dependency react"),
                    catalog.get("react").supported,
                )
            )
        if "next" in node_dependencies:
            matches.append(
                _versioned_match(
                    "nextjs",
                    node_dependencies["next"],
                    _evidence(repository, package_path, "dependency next"),
                    catalog.get("nextjs").supported,
                )
            )

    python_evidence, python_dependencies = _python_project(target_root, repository)
    if python_evidence is not None:
        matches.append(_match("python", python_evidence))
        for dependency, profile in (("fastapi", "fastapi"), ("django", "django")):
            if dependency in python_dependencies:
                matches.append(
                    _versioned_match(
                        profile,
                        python_dependencies[dependency],
                        Evidence(
                            "dependency",
                            python_evidence.path,
                            f"dependency {dependency}",
                        ),
                        catalog.get(profile).supported,
                    )
                )

    if not matches:
        matches.append(
            ProfileMatch(
                profile="generic",
                verdict="confirmed",
                evidence=(Evidence("fallback", ".", "no supported stack evidence"),),
            )
        )
    order = {name: index for index, name in enumerate(catalog.profiles)}
    return tuple(sorted(matches, key=lambda item: order.get(item.profile, 999)))


def _node_dependencies(package: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        table = package.get(key, {})
        if isinstance(table, dict):
            for name, value in table.items():
                if isinstance(name, str) and isinstance(value, str):
                    result[canonicalize_name(name)] = value
    return result


def _python_project(target: Path, repository: Path) -> tuple[Evidence | None, dict[str, str]]:
    path = target / "pyproject.toml"
    if not path.is_file():
        return None, {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, {}
    dependencies: dict[str, str] = {}
    project = raw.get("project")
    poetry = raw.get("tool", {}).get("poetry") if isinstance(raw.get("tool"), dict) else None
    if isinstance(project, dict):
        for value in project.get("dependencies", []):
            if isinstance(value, str):
                try:
                    requirement = Requirement(value)
                except InvalidRequirement:
                    continue
                dependencies[canonicalize_name(requirement.name)] = str(requirement.specifier)
        confirmed = True
    elif isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
        table = poetry.get("dependencies", {})
        if isinstance(table, dict):
            for name, value in table.items():
                if name != "python" and isinstance(value, str):
                    dependencies[canonicalize_name(name)] = value
        confirmed = True
    else:
        confirmed = False
    if not confirmed:
        return None, {}
    return _evidence(repository, path, "Python project metadata"), dependencies


def _json_table(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _match(profile: str, evidence: Evidence) -> ProfileMatch:
    return ProfileMatch(profile=profile, verdict="confirmed", evidence=(evidence,))


def _versioned_match(
    profile: str, raw_version: str, evidence: Evidence, supported: str
) -> ProfileMatch:
    detected = _representative_version(raw_version)
    if detected is None or not supported:
        return ProfileMatch(profile, "confirmed", (evidence,), detected_version=detected)
    try:
        declared = SpecifierSet(raw_version)
        compatible = _specifier_sets_overlap(declared, SpecifierSet(supported))
    except (InvalidVersion, InvalidSpecifier):
        try:
            compatible = Version(detected) in SpecifierSet(supported)
        except (InvalidVersion, InvalidSpecifier):
            compatible = True
    if compatible:
        return ProfileMatch(profile, "confirmed", (evidence,), detected_version=detected)
    return ProfileMatch(
        profile,
        "unsupported",
        (evidence,),
        detected_version=detected,
        warning=f"detected {profile} {detected}; supported range is {supported}",
    )


def _representative_version(raw: str) -> str | None:
    lower_bound = re.search(r"(?:===|==|~=|>=|>)\s*(\d+(?:\.\d+){0,2})", raw)
    if lower_bound:
        return lower_bound.group(1)
    match = re.search(r"(?<!\d)(\d+(?:\.\d+){0,2})", raw)
    return match.group(1) if match else None


def _specifier_sets_overlap(left: SpecifierSet, right: SpecifierSet) -> bool:
    """Check practical release candidates around every declared boundary."""
    candidates = {Version("0"), Version("1")}
    for specifier_set in (left, right):
        for specifier in specifier_set:
            try:
                boundary = Version(specifier.version.rstrip(".*"))
            except InvalidVersion:
                continue
            candidates.add(boundary)
            release = list(boundary.release)
            for index in range(len(release)):
                below = release.copy()
                if below[index] > 0:
                    below[index] -= 1
                    candidates.add(Version(".".join(map(str, below[: index + 1]))))
                above = release.copy()
                above[index] += 1
                candidates.add(Version(".".join(map(str, above[: index + 1]))))
            candidates.add(Version(f"{boundary}.1"))
    return any(candidate in left and candidate in right for candidate in candidates)


def _evidence(repository: Path, path: Path, detail: str) -> Evidence:
    return Evidence("file", path.resolve().relative_to(repository).as_posix(), detail)


__all__ = ["detect_profiles"]
