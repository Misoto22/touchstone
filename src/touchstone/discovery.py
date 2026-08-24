"""Discover project facts that should not be copied into configuration by hand."""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from touchstone.config import ConfigError
from touchstone.execution import Executor
from touchstone.profiles.targets import TargetDiscovery, affected_targets, discover_targets

SchedulerName = Literal["launchd", "systemd", "unsupported"]


@dataclass(frozen=True, slots=True)
class ProjectDiscovery:
    root: Path
    slug: str
    default_branch: str
    engines: tuple[str, ...]
    scheduler: SchedulerName


def discover_project(start: Path, executor: Executor) -> ProjectDiscovery:
    root_result = executor.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"], timeout=30
    )
    if not root_result.ok or not root_result.stdout.strip():
        raise ConfigError(f"{start} is not inside a Git repository")
    root = Path(root_result.stdout.strip()).resolve()

    remote_result = executor.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"], timeout=30
    )
    if not remote_result.ok:
        raise ConfigError("the Git repository has no readable origin remote")
    slug = _github_slug(remote_result.stdout.strip())

    head = executor.run(
        [
            "git",
            "-C",
            str(root),
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
        timeout=30,
    )
    if head.ok and head.stdout.strip().startswith("origin/"):
        default_branch = head.stdout.strip().removeprefix("origin/")
    else:
        current = executor.run(["git", "-C", str(root), "branch", "--show-current"], timeout=30)
        default_branch = current.stdout.strip() if current.ok else ""
    if not default_branch:
        raise ConfigError("could not discover the repository's default branch")

    engines = tuple(name for name in ("codex", "claude") if shutil.which(name))
    scheduler: SchedulerName
    if sys.platform == "darwin":
        scheduler = "launchd"
    elif sys.platform.startswith("linux"):
        scheduler = "systemd"
    else:
        scheduler = "unsupported"
    return ProjectDiscovery(root, slug, default_branch, engines, scheduler)


def _github_slug(remote: str) -> str:
    scp = re.fullmatch(r"git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?", remote)
    if scp:
        return scp.group("slug")

    parsed = urlparse(remote)
    if parsed.hostname != "github.com":
        raise ConfigError("origin must be a GitHub origin such as owner/repository")
    slug = parsed.path.strip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    if len(slug.split("/")) != 2:
        raise ConfigError("origin must identify one GitHub owner/repository")
    return slug


__all__ = [
    "ProjectDiscovery",
    "SchedulerName",
    "TargetDiscovery",
    "affected_targets",
    "discover_project",
    "discover_targets",
]
