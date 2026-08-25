from __future__ import annotations

from touchstone.nodes import classify


class LoopWithoutProjectProtection:
    protected_paths: tuple[str, ...] = ()


def test_builtin_control_paths_cannot_be_removed_by_project_config() -> None:
    protected = classify._protected_paths(LoopWithoutProjectProtection())

    assert ".github/" in protected
    assert "touchstone.toml" in protected
    assert "**/migrations/" in protected
    assert "**/schema.*" in protected


def test_protected_path_globs_match_environment_files() -> None:
    assert classify._matches_path(".env.production", ".env.*")
    assert classify._matches_path("services/api/.env.local", "**/.env.*")


def test_a_source_prefix_matches_on_a_path_boundary_not_a_string_prefix() -> None:
    # `apps/web/apple.ts` is not a change under `apps/web/app`.
    assert classify._under("apps/web/app/page.tsx", "apps/web/app")
    assert classify._under("apps/web/app/page.tsx", "apps/web/app/")
    assert not classify._under("apps/web/apple.ts", "apps/web/app")
    assert not classify._under("apps/website/page.tsx", "apps/web")


def test_a_source_prefix_accepts_the_file_it_names() -> None:
    assert classify._under("docs/graph.md", "docs/graph.md")
    assert classify._under("src/touchstone/cli.py", "./src/")
    assert classify._under("anything.py", ".")
