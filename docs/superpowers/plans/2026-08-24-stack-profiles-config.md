# Stack Profiles and Configuration v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explainable stack detection, monorepo Project Targets, materialized Profile configuration, and a backward-compatible two-file schema v2.

**Architecture:** Pure detector modules read repository files through bounded path rules and return typed evidence without importing project code. Built-in and repository-local declarative Profiles materialize into `.touchstone/generated.toml`; the existing root config remains project-owned and overlays generated values through one normalized loader.

**Tech Stack:** Python 3.12+, dataclasses, `tomllib`, `tomli-w`, `packaging`, pytest

**Spec:** `docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md`

## Global Constraints

- Schema v1 keeps its current meaning and remains loadable.
- Detection never imports configuration, runs a command, calls a model, or follows excluded trees.
- Built-in Profiles are package resources and work from an installed wheel.
- Root `touchstone.toml` is project-owned; `.touchstone/generated.toml` is machine-owned.
- Ambiguous evidence fails non-interactive initialization with actionable choices.
- Every behavior starts with a failing test and ends with a focused commit.

---

### Task 1: Typed schema v2 and two-file loading

**Files:**
- Create: `src/touchstone/config_v2.py`
- Modify: `src/touchstone/config.py`
- Modify: `src/touchstone/migrate.py`
- Test: `tests/test_config_v2.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Produces: `load_v2(root_path: Path, raw: dict[str, Any]) -> Config`
- Produces: `merge_generated(generated: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]`
- Produces: `preview_v2_migration(path: Path, timezone: str, hourly_minute: int) -> MigrationPreview`
- Preserves: `touchstone.config.load()` and all schema-v1 tests

- [ ] **Step 1: Write failing v1 compatibility and v2 ownership tests**

```python
def test_v1_still_loads_without_generated_file(v1_config: Path) -> None:
    assert load(v1_config).source.schema_version == 1


def test_v2_loads_generated_then_project_override(tmp_path: Path) -> None:
    generated = write_generated(tmp_path, target_path="apps/web", timeout=900)
    root = write_v2(tmp_path, generated=generated.name, timeout=1200)
    config = load(root)
    assert config.source.schema_version == 2
    assert config.engine.timeout_seconds == 1200
    assert config.targets["web"].path == Path("apps/web")


def test_generated_path_cannot_escape_repository(tmp_path: Path) -> None:
    root = write_v2(tmp_path, generated="../outside.toml")
    with pytest.raises(ConfigError, match="generated.*repository"):
        load(root)
```

- [ ] **Step 2: Run the focused tests and verify v2 is rejected**

Run: `.venv/bin/python -m pytest tests/test_config_v2.py tests/test_migrate.py -q`

Expected: FAIL because only schema version 1 is accepted.

- [ ] **Step 3: Define normalized v2 types and merge rules**

```python
@dataclass(frozen=True, slots=True)
class TargetConfig:
    id: str
    path: Path
    profiles: tuple[str, ...]
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GeneratedMetadata:
    package_version: str
    profile_versions: tuple[tuple[str, str], ...]
    source_digest: str
```

Keep v1 parsing in `config.py`. Dispatch version 2 to `config_v2.py`. Resolve both config files relative to the root config, reject symlink/path escape, deep-merge tables, deduplicate additive arrays, and let explicit root scalars win. Return one existing `Config` extended with timezone, targets, generated metadata, retry, and Actions settings.

- [ ] **Step 4: Add preview-only and explicit-write v1→v2 migration**

`MigrationPreview` contains root text, generated text, warnings, and the backup destination. `touchstone config migrate --check` prints the diff and writes nothing. `--write --timezone AREA --hourly-minute MM` writes a backup first, uses atomic replacements, and refuses an unconfirmed schedule-anchor change.

- [ ] **Step 5: Run config, migration, and package-resource tests**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_config_v2.py tests/test_migrate.py tests/test_package.py -q`

Expected: PASS.

- [ ] **Step 6: Commit schema v2**

```bash
git add src/touchstone/config.py src/touchstone/config_v2.py src/touchstone/migrate.py tests/test_config_v2.py tests/test_migrate.py
git commit -m "feat: add owned configuration schema v2"
```

### Task 2: Built-in Profile catalog and explainable evidence

**Files:**
- Create: `src/touchstone/profiles/__init__.py`
- Create: `src/touchstone/profiles/model.py`
- Create: `src/touchstone/profiles/catalog.py`
- Create: `src/touchstone/profiles/detect.py`
- Create: `src/touchstone/resources/profiles/*.toml`
- Modify: `pyproject.toml`
- Test: `tests/test_profiles.py`
- Test fixtures: `tests/fixtures/profiles/`

**Interfaces:**
- Produces: `detect_profiles(root: Path, target: TargetCandidate) -> tuple[ProfileMatch, ...]`
- Produces: `load_catalog(local_dir: Path | None = None) -> ProfileCatalog`
- Produces: `DetectionVerdict = Literal["confirmed", "candidate", "unsupported"]`

- [ ] **Step 1: Add failing evidence fixtures for every built-in Profile**

```python
@pytest.mark.parametrize(
    ("fixture", "confirmed"),
    [
        ("next-typescript", {"javascript", "typescript", "react", "nextjs"}),
        ("django-uv", {"python", "django"}),
        ("fastapi-poetry", {"python", "fastapi"}),
        ("react-library", {"javascript", "react"}),
        ("node-cli", {"javascript", "node"}),
    ],
)
def test_profile_evidence_is_composable(fixtures: Path, fixture: str, confirmed: set[str]) -> None:
    matches = detect_fixture(fixtures / fixture)
    assert {m.profile for m in matches if m.verdict == "confirmed"} == confirmed
    assert all(m.evidence for m in matches)
```

Also test that `package.json` alone confirms `javascript` but not `node`, `pyproject.toml` containing only Ruff settings does not confirm Python, and detector code never imports `next.config.*`, Django settings, or Python modules.

- [ ] **Step 2: Run tests and confirm the Profile package is absent**

Run: `.venv/bin/python -m pytest tests/test_profiles.py -q`

Expected: FAIL importing `touchstone.profiles`.

- [ ] **Step 3: Implement immutable evidence and catalog types**

```python
@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProfileMatch:
    profile: str
    verdict: DetectionVerdict
    evidence: tuple[Evidence, ...]
    detected_version: str | None = None
    warning: str = ""
```

Normalize Python distribution names with `packaging.utils.canonicalize_name`. Parse JSON/TOML/YAML-as-data only; do not execute JavaScript or Python. Treat unsupported framework majors as candidates that fall back to their base Profile.

- [ ] **Step 4: Package declarative built-ins and restricted local Profiles**

Each built-in TOML records Profile version, category, supported version range, audit context, protected/source paths, detection evidence clauses, and disabled Validation Candidates. Reject local Profile keys that request Python modules, shell detectors, URLs, or executable hooks.

- [ ] **Step 5: Run Profile, distribution, and wheel isolation tests**

Run: `.venv/bin/python -m pytest tests/test_profiles.py tests/test_distribution.py tests/test_package.py -q`

Expected: PASS with the built-in TOML resources present in the wheel.

- [ ] **Step 6: Commit Profile detection**

```bash
git add src/touchstone/profiles src/touchstone/resources/profiles pyproject.toml tests/test_profiles.py tests/fixtures/profiles
git commit -m "feat: detect composable stack profiles"
```

### Task 3: Workspace Targets and dependency expansion

**Files:**
- Create: `src/touchstone/profiles/targets.py`
- Modify: `src/touchstone/discovery.py`
- Test: `tests/test_targets.py`
- Test fixtures: `tests/fixtures/workspaces/`

**Interfaces:**
- Produces: `discover_targets(root: Path) -> TargetDiscovery`
- Produces: `TargetDiscovery.targets`, `.candidates`, `.excluded`, `.warnings`
- Produces: `affected_targets(changed_paths: Iterable[str], discovery: TargetDiscovery) -> tuple[str, ...]`

- [ ] **Step 1: Write failing mixed-monorepo tests**

```python
def test_explicit_workspace_members_become_stable_targets(mixed_workspace: Path) -> None:
    found = discover_targets(mixed_workspace)
    assert [(t.id, t.path) for t in found.targets] == [
        ("web", Path("apps/web")),
        ("api", Path("services/api")),
        ("ui", Path("packages/ui")),
    ]
    assert Path("examples/demo") in {c.path for c in found.candidates}


def test_changed_shared_package_expands_reverse_dependencies(mixed_workspace: Path) -> None:
    found = discover_targets(mixed_workspace)
    assert affected_targets(["packages/ui/button.tsx"], found) == ("ui", "web")
```

Test npm/Yarn workspaces, `pnpm-workspace.yaml`, uv members/excludes, PDM members, deepest-path ownership, root Workspace Container behavior, stable duplicate-ID suffixes, and exclusion of submodules/vendor/fixtures/venvs.

- [ ] **Step 2: Run tests and verify discovery lacks Target semantics**

Run: `.venv/bin/python -m pytest tests/test_targets.py tests/test_discovery.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement bounded workspace discovery**

Resolve workspace globs without leaving the Git root, require a member manifest, sort paths deterministically, derive suggested IDs from directory names, and surface collisions as candidates. Parse dependency edges only from direct workspace dependency declarations; never infer edges from imports or directory names.

- [ ] **Step 4: Implement affected-Target expansion**

Assign files to the deepest Target. Traverse reverse declared dependency edges with a visited set, keep the directly changed Target first, then stable Target ID order. A root container owns no files.

- [ ] **Step 5: Run Target, Profile, and acceptance tests**

Run: `.venv/bin/python -m pytest tests/test_targets.py tests/test_profiles.py tests/test_discovery.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Project Targets**

```bash
git add src/touchstone/discovery.py src/touchstone/profiles/targets.py tests/test_targets.py tests/fixtures/workspaces
git commit -m "feat: discover monorepo project targets"
```

### Task 4: Materialization, initialization, and refresh CLI

**Files:**
- Create: `src/touchstone/profiles/materialize.py`
- Modify: `src/touchstone/initialize.py`
- Modify: `src/touchstone/cli.py`
- Modify: `src/touchstone/doctor.py`
- Test: `tests/test_profile_materialization.py`
- Test: `tests/test_initialize.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Produces: `materialize(discovery: TargetDiscovery, matches: Mapping[str, tuple[ProfileMatch, ...]], catalog: ProfileCatalog) -> MaterializedConfig`
- Produces: `profile_diff(config: Config) -> ProfileDiff`
- Produces: CLI `profile detect`, `profile diff`, and `profile refresh`

- [ ] **Step 1: Write failing deterministic materialization tests**

```python
def test_init_writes_project_and_generated_files(next_repo: Path) -> None:
    report = initialize(init_options(next_repo), executor())
    assert report.root.name == "touchstone.toml"
    assert report.generated == next_repo / ".touchstone/generated.toml"
    assert load(report.root).targets["next-repo"].profiles == (
        "javascript",
        "typescript",
        "react",
        "nextjs",
    )


def test_refresh_preserves_root_overrides(next_repo: Path) -> None:
    initialized = initialize(init_options(next_repo), executor())
    add_root_override(initialized.root, timeout_seconds=1234)
    refresh_profiles(initialized.root, write=True)
    assert load(initialized.root).engine.timeout_seconds == 1234
```

Test semantic precedence, list deduplication, scalar conflict errors, candidate failure in non-interactive mode, explicit overrides, generic fallback, drift-only `--check`, atomic refresh, and no write when the diff is empty.

- [ ] **Step 2: Run initialization tests and verify the one-file renderer fails**

Run: `.venv/bin/python -m pytest tests/test_profile_materialization.py tests/test_initialize.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement materialization and provenance digest**

The generated file records package version, Profile versions, evidence paths/details, Target graph, generated context/path defaults, and Validation Candidates. Hash normalized generated data, not TOML formatting. The root file records project/forge/engine/Actions choices, Loop-to-Target bindings, schedules, and explicit overrides.

- [ ] **Step 4: Add CLI and doctor checks**

`profile detect --json` is read-only. `profile diff` exits 0 for no drift and 3 for drift. `profile refresh --check` never writes; `--write` prints the diff and atomically replaces only the generated file. Doctor emits stable checks for generated provenance, unsupported Profile version, ambiguous package manager, missing Target path, and edited generated content.

- [ ] **Step 5: Run all configuration/Profile tests**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_config_v2.py tests/test_profiles.py tests/test_targets.py tests/test_profile_materialization.py tests/test_initialize.py tests/test_doctor.py -q`

Expected: PASS.

- [ ] **Step 6: Commit initialization and refresh**

```bash
git add src/touchstone/profiles/materialize.py src/touchstone/initialize.py src/touchstone/cli.py src/touchstone/doctor.py tests/test_profile_materialization.py tests/test_initialize.py tests/test_doctor.py
git commit -m "feat: materialize profile configuration"
```
