from __future__ import annotations

from types import SimpleNamespace

from touchstone.nodes import classify


class LoopWithoutProjectProtection:
    protected_paths: tuple[str, ...] = ()


class ConfigWithoutHarness:
    harness = None


class ConfigWithHarness:
    def __init__(self, mode: str, entrypoint: str) -> None:
        self.harness = SimpleNamespace(mode=mode, entrypoint=entrypoint)


def test_builtin_control_paths_cannot_be_removed_by_project_config() -> None:
    protected = classify._protected_paths(LoopWithoutProjectProtection(), ConfigWithoutHarness())

    assert ".github/" in protected
    assert "touchstone.toml" in protected
    assert "**/migrations/" in protected
    assert "**/schema.*" in protected


def test_a_declared_harness_entrypoint_is_protected() -> None:
    """Only the root `AGENTS.md` was built in; a custom entrypoint was editable and unescalated."""
    protected = classify._protected_paths(
        LoopWithoutProjectProtection(), ConfigWithHarness("embedded", "docs/rules.md")
    )

    assert "docs/rules.md" in protected


def test_an_external_harness_entrypoint_is_not_a_repository_path() -> None:
    """An external entrypoint names a file in the snapshot, not in the repository under audit."""
    protected = classify._protected_paths(
        LoopWithoutProjectProtection(), ConfigWithHarness("external", "harness/00-INDEX.md")
    )

    assert "harness/00-INDEX.md" not in protected


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
