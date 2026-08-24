from __future__ import annotations

from touchstone.nodes import classify


class LoopWithoutProjectProtection:
    protected_paths: tuple[str, ...] = ()


def test_builtin_control_paths_cannot_be_removed_by_project_config() -> None:
    protected = classify._protected_paths(LoopWithoutProjectProtection())

    assert ".github/" in protected
    assert "touchstone.toml" in protected


def test_protected_path_globs_match_environment_files() -> None:
    assert classify._matches_path(".env.production", ".env.*")
    assert classify._matches_path("services/api/.env.local", "**/.env.*")
