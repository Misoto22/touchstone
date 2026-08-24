# Ready-to-Use Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a generic installed CLI whose config, initialization, diagnostics, and setup work without owner-specific source values.

**Architecture:** Configuration is normalized behind `load_config`, project facts behind `discover_project`, and first-run mutations behind `Setup`. The CLI only parses arguments and renders structured results returned by those modules.

**Tech Stack:** Python 3.12+, argparse, dataclasses, tomllib, pathlib, GitHub CLI, Hatchling, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-08-24-ready-to-use-design.md`

## Global Constraints

- Distribution name is `touchstone-agent`; the executable remains `touchstone`.
- License is Apache-2.0.
- Configuration schema version is exactly `1`; unknown keys fail validation.
- Relative paths resolve from the config file, and secrets never enter config output.
- Built-in briefs are wheel resources addressed as `builtin:<name>`.
- Existing `run`, `resume`, and `graph` commands remain compatible.
- Every production behavior is introduced through a failing test first.

---

### Task 1: Versioned configuration and built-in resources

**Files:**
- Create: `src/touchstone/resources/__init__.py`
- Create: `src/touchstone/resources/briefs/code-audit.md`
- Create: `src/touchstone/resources/briefs/harness-review.md`
- Create: `src/touchstone/resources/briefs/review.md`
- Modify: `src/touchstone/config.py`
- Modify: `pyproject.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `load_config(path: Path | None = None) -> Config`
- Produces: `resolve_brief(reference: str, config_path: Path) -> Traversable | Path`
- Preserves: `load(path)` as a compatibility alias for one minor release

- [ ] **Step 1: Write failing config tests**

```python
def test_relative_paths_are_resolved_from_the_config_file(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "nested" / "touchstone.toml", project_path="../repo")
    assert load_config(config_path).repo_path == (tmp_path / "repo").resolve()

def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    path = write_raw_config(tmp_path, VALID_CONFIG + '\n[engine]\nmodle = "wrong"\n')
    with pytest.raises(ConfigError, match="engine.modle"):
        load_config(path)

def test_builtin_brief_is_available_from_package_resources(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, brief="builtin:code-audit"))
    assert "Take the queue" in config.loop("code").prompt()
```

- [ ] **Step 2: Run the focused tests and confirm they fail because versioned loading and package resources do not exist**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

Expected: FAIL on missing `load_config` and unresolved `builtin:` brief.

- [ ] **Step 3: Implement strict tables, source tracking, and package-resource briefs**

```python
@dataclass(frozen=True, slots=True)
class ConfigSource:
    path: Path
    schema_version: int

def load_config(path: Path | None = None) -> Config:
    chosen = path or discover_config_path()
    raw = parse_toml(chosen)
    validate_known_keys(raw)
    require_schema_version(raw, expected=1)
    return normalize(raw, base_dir=chosen.parent.resolve(), source=chosen.resolve())
```

Move the three briefs rather than duplicating them. Resolve `builtin:<name>` with `importlib.resources.files("touchstone.resources.briefs")`. Include Markdown resources in the wheel through Hatchling artifacts configuration.

- [ ] **Step 4: Run config tests and the existing acceptance suite**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the resource and config contract**

```bash
git add pyproject.toml src/touchstone/config.py src/touchstone/resources tests/test_config.py briefs
git commit -m "feat: standardize project configuration"
```

### Task 2: Project discovery and generic initialization

**Files:**
- Create: `src/touchstone/discovery.py`
- Create: `src/touchstone/initialize.py`
- Modify: `src/touchstone/cli.py`
- Test: `tests/test_discovery.py`
- Test: `tests/test_initialize.py`

**Interfaces:**
- Produces: `discover_project(start: Path, executor: Executor) -> ProjectDiscovery`
- Produces: `initialize(options: InitOptions, executor: Executor) -> Path`
- Consumes: `load_config` and built-in brief identifiers from Task 1

- [ ] **Step 1: Write failing discovery and non-interactive init tests**

```python
def test_discovers_slug_and_default_branch_from_origin(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, remote="git@github.com:acme/widgets.git", branch="trunk")
    found = discover_project(repo, LocalExecutor())
    assert found.root == repo.resolve()
    assert found.slug == "acme/widgets"
    assert found.default_branch == "trunk"

def test_non_interactive_init_writes_a_loadable_generic_config(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, remote="https://github.com/acme/widgets.git")
    path = initialize(init_options(repo, engine="codex", model="gpt-test"), LocalExecutor())
    config = load_config(path)
    assert config.forge.slug == "acme/widgets"
    assert "/Users/" not in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run focused tests and confirm missing modules cause the expected failure**

Run: `.venv/bin/python -m pytest tests/test_discovery.py tests/test_initialize.py -q`

Expected: FAIL importing `touchstone.discovery` and `touchstone.initialize`.

- [ ] **Step 3: Implement discovery and deterministic TOML rendering**

```python
@dataclass(frozen=True, slots=True)
class ProjectDiscovery:
    root: Path
    slug: str
    default_branch: str
    engines: tuple[str, ...]
    scheduler: Literal["launchd", "systemd", "unsupported"]

@dataclass(frozen=True, slots=True)
class InitOptions:
    start: Path
    engine: EngineName
    model: str
    workflows: tuple[str, ...]
    schedule: str
    force: bool = False
```

Parse both SSH and HTTPS GitHub remotes. Determine the default branch from `refs/remotes/origin/HEAD`, then GitHub metadata, then the checked-out branch. Render stable TOML with no credentials or personal values. Refuse overwrite unless `force` is true.

- [ ] **Step 4: Add `touchstone init` interactive and non-interactive CLI paths**

```text
touchstone init
touchstone init --non-interactive --engine codex --model gpt-test \
  --workflow ci.yml --schedule hourly
```

Interactive input delegates to `initialize`; it does not construct TOML in `cli.py`.

- [ ] **Step 5: Verify CLI behavior and full tests**

Run: `.venv/bin/python -m pytest tests/test_discovery.py tests/test_initialize.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit discovery and initialization**

```bash
git add src/touchstone/discovery.py src/touchstone/initialize.py src/touchstone/cli.py tests/test_discovery.py tests/test_initialize.py
git commit -m "feat: initialize projects from discovered settings"
```

### Task 3: Structured doctor and idempotent setup

**Files:**
- Create: `src/touchstone/doctor.py`
- Create: `src/touchstone/setup.py`
- Modify: `src/touchstone/forge.py`
- Modify: `src/touchstone/cli.py`
- Test: `tests/test_doctor.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Produces: `run_doctor(config: Config, context: CheckContext) -> DoctorReport`
- Produces: `setup(config: Config, *, dry_run: bool) -> SetupReport`
- Adds: `Forge.repository_info()`, `Forge.labels()`, and `Forge.ensure_label()`

- [ ] **Step 1: Write failing behavior tests for diagnostic severity and dry-run setup**

```python
def test_doctor_fails_before_sessions_when_engine_is_missing(config: Config) -> None:
    report = run_doctor(config, check_context(commands=set(), forge=healthy_forge()))
    check = report.by_id("engine.command")
    assert (check.level, check.repair) == ("FAIL", "Install the configured codex command")

def test_setup_dry_run_reports_labels_without_mutation(config: Config) -> None:
    forge = recording_forge(existing_labels=set())
    report = setup(config, dry_run=True, forge=forge)
    assert report.planned_labels == ("touchstone:audit",)
    assert forge.created_labels == []
```

- [ ] **Step 2: Verify focused tests fail because the interfaces are missing**

Run: `.venv/bin/python -m pytest tests/test_doctor.py tests/test_setup.py -q`

Expected: FAIL on missing modules.

- [ ] **Step 3: Implement structured checks and rendering-neutral reports**

```python
@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    level: Literal["PASS", "WARN", "FAIL"]
    summary: str
    repair: str | None = None

@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def exit_code(self) -> int:
        return 1 if any(check.level == "FAIL" for check in self.checks) else 0
```

Run checks in stable order. Redact environment values. `setup` creates only state directories and labels, and returns the same planned actions in dry-run and real modes.

- [ ] **Step 4: Add `doctor [--json]` and `setup [--dry-run] [--json]` commands**

JSON uses dataclass fields and stable check IDs. Human output is one line per check plus repair commands.

- [ ] **Step 5: Run focused and regression tests**

Run: `.venv/bin/python -m pytest tests/test_doctor.py tests/test_setup.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit doctor and setup**

```bash
git add src/touchstone/doctor.py src/touchstone/setup.py src/touchstone/forge.py src/touchstone/cli.py tests/test_doctor.py tests/test_setup.py
git commit -m "feat: add project diagnostics and setup"
```

### Task 4: Generic package metadata and migration surface

**Files:**
- Create: `src/touchstone/migrate.py`
- Modify: `src/touchstone/config.py`
- Modify: `src/touchstone/cli.py`
- Modify: `touchstone.example.toml`
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Test: `tests/test_migrate.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Produces: `migrate_config(path: Path) -> MigrationReport`
- Produces: `touchstone config migrate PATH`

- [ ] **Step 1: Write failing migration and metadata tests**

```python
def test_migration_backs_up_unversioned_config_before_replacement(tmp_path: Path) -> None:
    source = write_legacy_config(tmp_path)
    report = migrate_config(source)
    assert report.backup.read_bytes() == LEGACY_CONFIG.encode()
    assert load_config(source).source.schema_version == 1

def test_distribution_uses_public_name_and_contains_briefs() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text())
    assert metadata["project"]["name"] == "touchstone-agent"
    assert resource_files().joinpath("briefs/code-audit.md").is_file()
```

- [ ] **Step 2: Run tests and confirm the absent migration and old package name fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py tests/test_package.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement backup-first migration and package metadata**

Use `<name>.v0.bak` as the non-overwriting backup suffix, write the replacement atomically through a sibling temporary file, and keep deprecated environment overrides with doctor warnings. Replace the Kioku example with the renderer's generic output. Ignore `.langgraph_api/`, state databases, dry-run diffs, build artifacts, and local configs.

- [ ] **Step 4: Run all foundation tests, lint, and graph check**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m touchstone.cli graph --check`

Expected: all commands exit 0.

- [ ] **Step 5: Commit the installable foundation**

```bash
git add .gitignore pyproject.toml touchstone.example.toml src/touchstone tests
git commit -m "feat: package the generic touchstone CLI"
```
