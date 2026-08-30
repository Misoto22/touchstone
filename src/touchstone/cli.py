"""The command line.

The stable command surface for initialization, diagnostics, scheduled runs,
human decisions, and graph documentation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from touchstone import __version__, visualise
from touchstone.config import ConfigError, load


def _config_path(args: argparse.Namespace) -> int:
    from touchstone.inspection import configuration_paths

    paths = {key: str(value) for key, value in configuration_paths(load(args.config)).items()}
    if args.json:
        print(json.dumps(paths, sort_keys=True))
    else:
        for owner, path in paths.items():
            print(f"{owner}: {path}")
    return 0


def _config_check(args: argparse.Namespace) -> int:
    from touchstone.harnesses import HarnessResolutionError, resolve_harness

    config = load(args.config)
    if config.harness is None:
        payload = {
            "status": "clean",
            "reason": "legacy-instruction-discovery",
            "detail": "no explicit [harness]; repository instruction discovery is unchanged",
        }
    else:
        try:
            context = resolve_harness(config)
        except HarnessResolutionError as exc:
            payload = {"status": "blocked", "reason": exc.reason_code, "detail": exc.detail}
            _print_payload(payload, json_output=args.json)
            return 3
        payload = {
            "status": "clean",
            "reason": "harness-resolved",
            "detail": f"{context.mode}:{context.source}@{context.revision}",
        }
    _print_payload(payload, json_output=args.json)
    return 0


def _config_show(args: argparse.Namespace) -> int:
    import tomli_w

    from touchstone.inspection import effective_configuration, redacted_configuration

    payload = (
        effective_configuration(load(args.config))
        if args.effective
        else redacted_configuration(load(args.config))
    )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif args.effective:
        for field, record in payload.items():
            print(f"{field} = {record['value']!r} ({record['source']})")
    else:
        print(tomli_w.dumps(payload), end="")
    return 0


def _config_explain(args: argparse.Namespace) -> int:
    from touchstone.inspection import explain_configuration_field

    payload = explain_configuration_field(load(args.config), args.field)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['field']} = {payload['value']!r}")
        print(f"source: {payload['source']}")
    return 0


def _harness_register(args: argparse.Namespace) -> int:
    from touchstone.harnesses import register_harness

    path = register_harness(args.source, args.path)
    print(f"registered {args.source} in {path}")
    return 0


def _harness_list(args: argparse.Namespace) -> int:
    from touchstone.harnesses import load_registry

    entries = {source: str(path) for source, path in load_registry().items()}
    if args.json:
        print(json.dumps(entries, sort_keys=True))
    else:
        for source, path in entries.items():
            print(f"{source}: {path}")
    return 0


def _harness_resolve(args: argparse.Namespace) -> int:
    from touchstone.harnesses import HarnessResolutionError, resolve_harness

    try:
        context = resolve_harness(load(args.config))
    except HarnessResolutionError as exc:
        _print_payload(
            {"status": "blocked", "reason": exc.reason_code, "detail": exc.detail},
            json_output=args.json,
        )
        return 3
    payload = {
        "status": "clean",
        "mode": context.mode,
        "source": context.source,
        "entrypoint": context.entrypoint.relative_to(context.context_root).as_posix(),
        "revision": context.revision,
        "evidence": list(context.evidence),
    }
    _print_payload(payload, json_output=args.json)
    return 0


def _harness_unregister(args: argparse.Namespace) -> int:
    from touchstone.harnesses import unregister_harness

    path = unregister_harness(args.source)
    print(f"unregistered {args.source} from {path}")
    return 0


def _print_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(" · ".join(f"{key}={value}" for key, value in payload.items()))


def _init(args: argparse.Namespace) -> int:
    from touchstone.execution.local import LocalExecutor
    from touchstone.initialize import InitOptions, initialize
    from touchstone.profiles.materialize import (
        ambiguous_package_managers,
        detect_package_managers,
    )

    executor = LocalExecutor()
    from touchstone.discovery import discover_project

    discovered = discover_project(args.path, executor)
    engine = args.engine
    model = args.model
    workflows = tuple(args.workflow or ())
    schedule = args.schedule
    package_manager = args.package_manager
    visibility = args.visibility
    wake_minutes = args.wake_minutes
    if args.non_interactive:
        if not engine or not model or not workflows or not schedule:
            raise ConfigError(
                "non-interactive init requires --engine, --model, --schedule, "
                "and at least one --workflow"
            )
        visibility = visibility or "public"
    else:
        engine = engine or input("Engine (codex or claude) [codex]: ").strip() or "codex"
        model = model or input("Model: ").strip()
        if not workflows:
            workflow = input("Required workflow [ci.yml]: ").strip() or "ci.yml"
            workflows = (workflow,)
        schedule = schedule or input("Schedule [hourly@00]: ").strip() or "hourly@00"
        visibility = (
            visibility
            or input("Repository visibility (public or private) [public]: ").strip()
            or "public"
        )
        default_wake = 15 if visibility == "public" else 60
        if wake_minutes is None:
            wake_raw = input(f"GitHub Actions wake minutes [{default_wake}]: ").strip()
            try:
                wake_minutes = int(wake_raw) if wake_raw else default_wake
            except ValueError:
                raise ConfigError("GitHub Actions wake minutes must be an integer") from None
        managers = detect_package_managers(discovered.root)
        ambiguous = ambiguous_package_managers(managers)
        if package_manager is None and ambiguous:
            choices = "/".join(ambiguous[0])
            package_manager = input(f"Package manager ({choices}): ").strip() or None
    if engine not in ("codex", "claude"):
        raise ConfigError("engine must be 'codex' or 'claude'")
    report = initialize(
        InitOptions(
            start=args.path,
            engine=engine,
            model=model or "",
            workflows=workflows,
            schedule=schedule or "hourly@00",
            timezone=args.timezone,
            visibility=visibility,
            wake_minutes=wake_minutes,
            profiles=tuple(args.profile or ()),
            package_manager=package_manager,
            output=args.output,
            force=args.force,
            discovered=discovered,
        ),
        executor,
    )
    print(f"wrote {report.root}")
    print(f"generated {report.generated}")
    return 0


def _profile_detect(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from touchstone.profiles.materialize import detect_repository

    discovery, matches, _catalog = detect_repository(args.path)
    payload = {
        "targets": [
            {
                "id": target.id,
                "path": target.path.as_posix(),
                "profiles": [
                    match.profile for match in matches[target.id] if match.verdict == "confirmed"
                ],
                "matches": [asdict(match) for match in matches[target.id]],
                "dependencies": list(target.dependencies),
            }
            for target in discovery.targets
        ],
        "candidates": [candidate.path.as_posix() for candidate in discovery.candidates],
        "excluded": [path.as_posix() for path in discovery.excluded],
        "warnings": list(discovery.warnings),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for target in payload["targets"]:
            print(f"{target['id']}: {', '.join(target['profiles'])}")
        for candidate in payload["candidates"]:
            print(f"candidate: {candidate}")
        for warning in payload["warnings"]:
            print(f"warning: {warning}")
    return 0


def _profile_diff(args: argparse.Namespace) -> int:
    from touchstone.profiles.materialize import profile_diff

    report = profile_diff(load(args.config))
    print(report.diff if report.changed else "generated Profile configuration is current")
    return 3 if report.changed else 0


def _profile_refresh(args: argparse.Namespace) -> int:
    from touchstone.profiles.materialize import refresh_profiles

    config = load(args.config)
    report = refresh_profiles(config.source.path, write=args.write)
    if report.diff:
        print(report.diff)
    if report.written:
        print(f"wrote {report.path}")
    elif not report.changed:
        print("generated Profile configuration is current")
    return 3 if report.changed and not args.write else 0


def _validate(args: argparse.Namespace) -> int:
    from touchstone import execution
    from touchstone.validation import prepare, validate

    config = load(args.config)
    if args.target:
        targets = tuple(args.target)
    elif args.loop:
        targets = config.loop(args.loop).targets
    else:
        targets = tuple(config.targets)
    executor = execution.build(config)
    preparation = prepare(config, targets, executor)
    for result in preparation.results:
        print(f"prepare {result.target}: {result.reason} — {' '.join(result.argv)}")
    if preparation.outcome == "blocked":
        return 3
    report = validate(config, targets, executor)
    for result in report.results:
        print(f"validate {result.target}: {result.reason} — {' '.join(result.argv)}")
        if result.reason not in {"passed", "disabled"}:
            detail = (result.stderr or result.stdout).strip()
            if detail:
                print(f"  {detail[-400:]}")
    return 3 if report.blocked else 0


def _doctor(args: argparse.Namespace) -> int:
    from touchstone.doctor import build_context, run_doctor

    config = load(args.config)
    report = run_doctor(config, build_context(config, offline=args.offline))
    if args.json:
        print(report.to_json())
    else:
        for check in report.checks:
            print(f"{check.level:4} {check.id}: {check.summary}")
            if check.repair:
                print(f"     repair: {check.repair}")
    return report.exit_code


def _setup(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from touchstone.setup import setup

    config = load(args.config)
    report = setup(config, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        action = "would configure" if args.dry_run else "configured"
        print(f"{action} state at {config.state_dir}")
        for label in report.planned_labels:
            print(f"  label: {label}")
    return 0


def _sync(args: argparse.Namespace) -> int:
    from touchstone.execution.local import LocalExecutor
    from touchstone.fleet import (
        FleetError,
        load_project,
        render_compose,
        sync_check,
        sync_propose,
    )

    try:
        project = load_project(Path(args.project))
    except FleetError as exc:
        print(f"touchstone: {exc}", file=sys.stderr)
        return 78
    if args.compose:
        destination = Path(args.compose)
        # Compose resolves a relative volume source against the file's own
        # directory, so the file has to know where it is being written.
        rendered = render_compose(project, base=destination.parent)
        if destination.exists() and destination.read_text(encoding="utf-8") == rendered:
            print(f"{destination}: current")
            return 0
        if args.check:
            print(f"{destination}: drifted")
            return 3
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        print(f"wrote {destination}")
        return 0
    if args.pr:
        report = sync_propose(project, LocalExecutor(), branch_prefix="touchstone/fleet-")
        for slug, branch in report.proposed:
            print(f"{slug}: opened a pull request from {branch}")
        for slug in report.unchanged:
            print(f"{slug}: current")
        for slug, detail in report.failed:
            print(f"{slug}: {detail}", file=sys.stderr)
        return report.exit_code
    check = sync_check(project)
    for slug in check.matched:
        print(f"{slug}: current")
    for slug in check.drifted:
        state = "absent" if slug in check.missing else "drifted"
        print(f"{slug}: {state}")
    if check.drifted:
        print(
            f"{len(check.drifted)} member(s) differ from the {project.name!r} project; "
            "run 'touchstone sync --pr' to propose the change",
            file=sys.stderr,
        )
    return check.exit_code


def _actions_init(args: argparse.Namespace) -> int:
    from touchstone.hosted.workflow import (
        ActionPins,
        actions_diff,
        render_workflow,
        resolve_action_sha,
    )

    config = load(args.config)
    action_sha = args.action_sha or resolve_action_sha(config)
    report = actions_diff(
        config.repo_path,
        render_workflow(config, ActionPins(), action_sha=action_sha),
    )
    if report.changed:
        print(report.diff)
        if args.check:
            return 3
        report.write()
        print(f"wrote {report.path}")
    else:
        print("GitHub Actions workflow is current")
    return 0


def _actions_setup(args: argparse.Namespace) -> int:
    import getpass

    from touchstone.hosted.app_setup import ActionsSetup, SetupOptions

    config = load(args.config)
    manual_code = getpass.getpass("One-time GitHub App manifest code: ") if args.manual_code else ""
    report = ActionsSetup(config).run(
        SetupOptions(
            check=args.check,
            owner_type="organization" if args.organization else "user",
            callback_port=args.callback_port,
            callback_timeout_seconds=args.callback_timeout,
            manual_code=manual_code,
        )
    )
    print(f"GitHub Actions setup: {report.state} ({report.step})")
    if report.repair:
        print(f"repair: {report.repair}")
    return 0 if report.state == "complete" else 3


def _hosted(args: argparse.Namespace) -> int:
    from touchstone.hosted.runtime import (
        CandidateIntegrityError,
        install_stage,
        run_stage,
    )

    config = load(args.config)
    try:
        if args.stage == "install":
            install_stage(config, for_stage=args.for_stage)
            return 0
        result = run_stage(config, args.stage)
    except CandidateIntegrityError as exc:
        print(f"touchstone hosted: {exc}", file=sys.stderr)
        return 3
    # Say why on the way out. A blocked or failed stage used to return its exit
    # code silently, so a runner log showed sixteen seconds of nothing and
    # "Process completed with exit code 3"; the reason existed only inside an
    # uploaded artifact, which is not where anyone looks first.
    print(
        _hosted_summary(args.stage, result),
        file=sys.stderr if result.outcome != "completed" else sys.stdout,
    )
    if result.outcome == "blocked":
        return 3
    if result.outcome == "failed":
        return 1
    return 0


def _hosted_summary(stage: str, result) -> str:  # type: ignore[no-untyped-def]
    parts = [f"touchstone {stage}: {result.outcome}"]
    for label, value in (
        ("loop", result.loop),
        ("reason", result.reason_code),
        ("change", result.change_state),
        ("candidate", result.candidate_id),
        ("clean start", result.clean_start_reason),
    ):
        if value:
            parts.append(f"{label}={value}")
    if result.partial:
        parts.append("partial=true")
    return " · ".join(parts)


def _status(args: argparse.Namespace) -> int:
    import json

    from touchstone.nodes.context import configure
    from touchstone.status import collect_status

    config = load(args.config)
    report = collect_status(config, configure(config), scheduler=_scheduler(config))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    if not report.findings:
        print("no recorded findings")
    for finding in report.findings:
        pull = f" #{finding['pr']}" if finding.get("pr") is not None else ""
        print(f"{finding['loop']}: {finding['state']}{pull} — {finding['title']}")
    for run in report.last_runs:
        print(f"last {run.get('loop', 'unknown')}: {run.get('outcome', 'unknown')}")
    if report.scheduler:
        installed = len(report.scheduler["installed"])
        missing = len(report.scheduler["missing"])
        print(
            f"scheduler: {report.scheduler['adapter']} ({installed} installed, {missing} missing)"
        )
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    import json

    from touchstone.nodes.context import configure
    from touchstone.status import reconcile_status

    config = load(args.config)
    report = reconcile_status(config, configure(config))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for loop, fields in report.items():
            summary = ", ".join(
                f"{name}={len(values)}" for name, values in fields.items() if values
            )
            print(f"{loop}: {summary or 'no lifecycle changes'}")
    failed = any(
        fields["failed"] or fields["inconclusive"] or fields["partial_unresolved"]
        for fields in report.values()
    )
    return 1 if failed else 0


def _scheduler(config):  # type: ignore[no-untyped-def]
    from touchstone.execution.local import LocalExecutor
    from touchstone.scheduling import current_scheduler

    try:
        # The configured executor owns repository/model work. Native user
        # timers belong to the machine running this CLI, even when that work
        # is delegated to an SSH target.
        return current_scheduler(LocalExecutor())
    except RuntimeError as exc:
        raise ConfigError(str(exc)) from None


def _install_scheduler(args: argparse.Namespace) -> int:
    import json

    config = load(args.config)
    try:
        report = _scheduler(config).install(config, target=args.output, dry_run=args.dry_run)
    except RuntimeError as exc:
        raise ConfigError(str(exc)) from None
    payload = {
        "files": [str(path) for path in report.files],
        "changed": [str(path) for path in report.changed],
        "commands": list(report.commands),
        "dry_run": args.dry_run,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        verb = "would write" if args.dry_run else "installed"
        for path in report.files:
            print(f"{verb}: {path}")
        for command in report.commands:
            print(f"inspect: {command}")
    return 0


def _uninstall_scheduler(args: argparse.Namespace) -> int:
    import json

    config = load(args.config)
    try:
        report = _scheduler(config).uninstall(config, target=args.output, dry_run=args.dry_run)
    except RuntimeError as exc:
        raise ConfigError(str(exc)) from None
    payload = {
        "files": [str(path) for path in report.files],
        "removed": [str(path) for path in report.changed],
        "dry_run": args.dry_run,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        verb = "would remove" if args.dry_run else "removed"
        for path in report.changed:
            print(f"{verb}: {path}")
    return 0


def _scheduler_status(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    config = load(args.config)
    status = _scheduler(config).status(config)
    payload = asdict(status)
    payload["installed"] = [str(path) for path in status.installed]
    payload["missing"] = [str(path) for path in status.missing]
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"scheduler: {status.adapter}")
        for path in status.installed:
            print(f"installed: {path}")
        for path in status.missing:
            print(f"missing: {path}")
    return 0


def _migrate_config(args: argparse.Namespace) -> int:
    from touchstone.migrate import migrate_config

    report = migrate_config(args.path)
    print(f"migrated {report.path} from version {report.from_version} to {report.to_version}")
    print(f"backup: {report.backup}")
    return 0


def _migrate_config_v2(args: argparse.Namespace) -> int:
    import difflib

    from touchstone.migrate import apply_v2_migration, preview_v2_migration

    preview = preview_v2_migration(
        args.path,
        timezone=args.timezone,
        hourly_minute=args.hourly_minute,
    )
    current = preview.path.read_text(encoding="utf-8")
    root_diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        preview.root_text.splitlines(keepends=True),
        fromfile=str(preview.path),
        tofile=str(preview.path),
    )
    generated_diff = difflib.unified_diff(
        (),
        preview.generated_text.splitlines(keepends=True),
        fromfile="/dev/null",
        tofile=str(preview.generated_path),
    )
    print("".join((*root_diff, *generated_diff)))
    for warning in preview.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not args.write:
        return 3
    report = apply_v2_migration(preview)
    print(f"migrated {report.path} from version 1 to version 2")
    print(f"generated: {report.generated}")
    print(f"backup: {report.backup}")
    return 0


def _run(args: argparse.Namespace) -> int:
    from touchstone.runner import execute

    config = load(args.config)
    print(f"touchstone: {config.describe(args.loop)}", file=sys.stderr)
    return execute(config, loop=args.loop, dry_run=args.dry_run)


def _run_due(args: argparse.Namespace) -> int:
    import datetime as dt

    from touchstone.runner import run_due

    config = load(args.config)
    try:
        report = run_due(
            config,
            now=dt.datetime.now(dt.UTC),
            loop=args.loop,
            force=args.force,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    for name, result in zip(report.started, report.results, strict=True):
        lifecycle = f" / {result.lifecycle.value}" if result.lifecycle else ""
        print(f"{name}: {result.outcome.value}{lifecycle}")
    if report.remaining_due:
        print(f"remaining due: {', '.join(report.remaining_due)}")
    return report.exit_code


def _resume(args: argparse.Namespace) -> int:
    from touchstone.runner import resume

    config = load(args.config)
    return resume(config, thread=args.thread, answer=args.answer)


def _graph(args: argparse.Namespace) -> int:
    root = Path.cwd()
    if args.write:
        print(f"wrote {visualise.write(root)}")
        return 0
    if args.check:
        ok, message = visualise.check(root)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if args.ascii:
        print(visualise.ascii_art())
        return 0
    print(visualise.mermaid())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="touchstone", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help="a TOML file; discovered when omitted")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="discover a repository and write touchstone.toml")
    init.add_argument("--path", type=Path, default=Path.cwd(), help="repository or child path")
    init.add_argument("--output", type=Path, help="configuration destination")
    init.add_argument("--non-interactive", action="store_true")
    init.add_argument("--engine", choices=("codex", "claude"))
    init.add_argument("--model")
    init.add_argument("--workflow", action="append", help="required default-branch workflow")
    init.add_argument("--schedule", help="hourly@MM, daily@HH:MM, or weekly@DAY,HH:MM")
    init.add_argument("--timezone", default="UTC", help="repository IANA timezone")
    init.add_argument(
        "--visibility",
        choices=("public", "private"),
        help="repository visibility used by hosted diagnostics",
    )
    init.add_argument(
        "--wake-minutes",
        type=int,
        choices=(5, 10, 15, 20, 30, 60),
        help="GitHub Actions wake cadence (default: public 15, private 60)",
    )
    init.add_argument("--profile", action="append", help="explicit Profile selection")
    init.add_argument("--package-manager", help="resolve ambiguous lockfile evidence")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    init.set_defaults(handler=_init)

    profile = sub.add_parser("profile", help="inspect or refresh detected stack Profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_detect = profile_sub.add_parser("detect", help="detect Targets and Profiles")
    profile_detect.add_argument("--path", type=Path, default=Path.cwd())
    profile_detect.add_argument("--json", action="store_true")
    profile_detect.set_defaults(handler=_profile_detect)
    profile_diff = profile_sub.add_parser("diff", help="show generated Profile drift")
    profile_diff.set_defaults(handler=_profile_diff)
    profile_refresh = profile_sub.add_parser("refresh", help="regenerate Profile configuration")
    refresh_mode = profile_refresh.add_mutually_exclusive_group()
    refresh_mode.add_argument("--check", action="store_true", help="report drift without writing")
    refresh_mode.add_argument("--write", action="store_true", help="replace generated config")
    profile_refresh.set_defaults(handler=_profile_refresh)

    validate = sub.add_parser("validate", help="run enabled structured Validation Gates")
    validate.add_argument("loop", nargs="?", help="use this Loop's configured Targets")
    validate.add_argument("--target", action="append", help="validate only this Target")
    validate.set_defaults(handler=_validate)

    doctor = sub.add_parser("doctor", help="check prerequisites without changing them")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--offline", action="store_true", help="skip GitHub network checks")
    doctor.set_defaults(handler=_doctor)

    setup = sub.add_parser("setup", help="create state and configured GitHub labels")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(handler=_setup)

    actions = sub.add_parser("actions", help="manage the GitHub-hosted execution backend")
    actions_sub = actions.add_subparsers(dest="actions_command", required=True)
    actions_init = actions_sub.add_parser("init", help="render the repository-owned workflow")
    actions_init.add_argument(
        "--action-sha", help="immutable Touchstone Action commit (40 hexadecimal characters)"
    )
    actions_init.add_argument(
        "--check", action="store_true", help="report drift without writing (exit 3 on drift)"
    )
    actions_init.set_defaults(handler=_actions_init)
    actions_setup = actions_sub.add_parser(
        "setup", help="create and install the owner-controlled publishing App"
    )
    actions_setup.add_argument("--check", action="store_true", help="inspect without mutation")
    actions_setup.add_argument(
        "--organization", action="store_true", help="create the App under the repository owner org"
    )
    actions_setup.add_argument("--callback-port", type=int, default=8917)
    actions_setup.add_argument("--callback-timeout", type=int, default=300)
    actions_setup.add_argument(
        "--manual-code",
        action="store_true",
        help="prompt for a one-time manifest code instead of using loopback callback",
    )
    actions_setup.set_defaults(handler=_actions_setup)

    hosted = sub.add_parser("hosted", help="run an internal GitHub-hosted trust stage")
    hosted.add_argument(
        "stage", choices=("install", "prepare", "analysis", "verify", "publish", "snapshot")
    )
    hosted.add_argument(
        "--for-stage",
        choices=("prepare", "analysis", "verify", "publish", "snapshot"),
        default="analysis",
        help="the credential-mapped stage this credential-free install prepares",
    )
    hosted.set_defaults(handler=_hosted)

    status = sub.add_parser("status", help="read repository lifecycle state without mutation")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_status)

    reconcile = sub.add_parser("reconcile", help="compare lifecycle state with GitHub")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(handler=_reconcile)

    install_scheduler = sub.add_parser(
        "install-scheduler", help="install native per-user loop timers"
    )
    install_scheduler.add_argument("--dry-run", action="store_true")
    install_scheduler.add_argument("--output", type=Path, help="render without enabling")
    install_scheduler.add_argument("--json", action="store_true")
    install_scheduler.set_defaults(handler=_install_scheduler)

    uninstall_scheduler = sub.add_parser(
        "uninstall-scheduler", help="remove native per-user loop timers"
    )
    uninstall_scheduler.add_argument("--dry-run", action="store_true")
    uninstall_scheduler.add_argument("--output", type=Path, help="remove rendered files here")
    uninstall_scheduler.add_argument("--json", action="store_true")
    uninstall_scheduler.set_defaults(handler=_uninstall_scheduler)

    scheduler_status = sub.add_parser(
        "scheduler-status", help="report native timer installation state"
    )
    scheduler_status.add_argument("--json", action="store_true")
    scheduler_status.set_defaults(handler=_scheduler_status)

    config = sub.add_parser("config", help="inspect or migrate configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_path = config_sub.add_parser("path", help="show every configuration source path")
    config_path.add_argument("--json", action="store_true")
    config_path.set_defaults(handler=_config_path)
    config_check = config_sub.add_parser("check", help="validate config and resolve its Harness")
    config_check.add_argument("--json", action="store_true")
    config_check.set_defaults(handler=_config_check)
    config_show = config_sub.add_parser("show", help="show redacted configuration")
    config_show.add_argument("--effective", action="store_true", help="include value ownership")
    config_show.add_argument("--json", action="store_true")
    config_show.set_defaults(handler=_config_show)
    config_explain = config_sub.add_parser("explain", help="explain one effective field")
    config_explain.add_argument("field")
    config_explain.add_argument("--json", action="store_true")
    config_explain.set_defaults(handler=_config_explain)
    migrate = config_sub.add_parser("migrate", help="migrate an unversioned config")
    migrate.add_argument("path", type=Path)
    migrate.set_defaults(handler=_migrate_config)
    migrate_v2 = config_sub.add_parser(
        "migrate-v2", help="preview or apply schema-v1 to schema-v2 migration"
    )
    migrate_v2.add_argument("path", type=Path)
    migrate_v2.add_argument("--timezone", default="UTC", help="repository IANA timezone")
    migrate_v2.add_argument(
        "--hourly-minute", type=int, default=0, help="anchor legacy hourly schedules (0-59)"
    )
    migrate_mode = migrate_v2.add_mutually_exclusive_group()
    migrate_mode.add_argument("--check", action="store_true", help="preview without writing")
    migrate_mode.add_argument("--write", action="store_true", help="back up and migrate")
    migrate_v2.set_defaults(handler=_migrate_config_v2)

    harness = sub.add_parser("harness", help="manage and resolve project Harness sources")
    harness_sub = harness.add_subparsers(dest="harness_command", required=True)
    harness_register = harness_sub.add_parser(
        "register", help="map a canonical Harness source to a local checkout"
    )
    harness_register.add_argument("source")
    harness_register.add_argument("--path", type=Path, required=True)
    harness_register.set_defaults(handler=_harness_register)
    harness_list = harness_sub.add_parser("list", help="list machine-local Harness mappings")
    harness_list.add_argument("--json", action="store_true")
    harness_list.set_defaults(handler=_harness_list)
    harness_resolve = harness_sub.add_parser(
        "resolve", help="resolve the configured Harness exactly as a run would"
    )
    harness_resolve.add_argument("--json", action="store_true")
    harness_resolve.set_defaults(handler=_harness_resolve)
    harness_unregister = harness_sub.add_parser(
        "unregister", help="remove one machine-local Harness mapping"
    )
    harness_unregister.add_argument("source")
    harness_unregister.set_defaults(handler=_harness_unregister)

    run = sub.add_parser("run", help="one iteration of a loop")
    run.add_argument("loop", help="which loop, by its [loop.*] name")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="audit, classify and review for real; stop before publishing",
    )
    run.set_defaults(handler=_run)

    run_due = sub.add_parser("run-due", help="claim and run currently due Loops")
    run_due.add_argument("--loop", help="restrict evaluation to one Loop")
    run_due.add_argument("--force", action="store_true", help="create a manual Due Slot")
    run_due.set_defaults(handler=_run_due)

    resume = sub.add_parser("resume", help="answer a parked draft and continue that thread")
    resume.add_argument("thread", help="the thread id the parked run reported")
    resume.add_argument("answer", choices=("approve", "close", "reanalyze"))
    resume.set_defaults(handler=_resume)

    sync = sub.add_parser(
        "sync", help="render a project's shared configuration for its member repositories"
    )
    sync.add_argument("--project", required=True, help="path to the project configuration")
    sync.add_argument(
        "--check",
        action="store_true",
        help="read-only; exit 3 when a member differs from what the project renders",
    )
    sync.add_argument(
        "--pr",
        action="store_true",
        help="propose each drifted member's fragment as a pull request",
    )
    sync.add_argument(
        "--compose",
        metavar="PATH",
        help="write a Compose file with one container per member repository",
    )
    sync.set_defaults(handler=_sync)

    graph = sub.add_parser("graph", help="draw the graph")
    graph.add_argument("--write", action="store_true", help=f"regenerate {visualise.DIAGRAM}")
    graph.add_argument(
        "--check", action="store_true", help="fail if the committed diagram is stale"
    )
    graph.add_argument(
        "--ascii", action="store_true", help="ASCII instead of Mermaid (needs grandalf)"
    )
    graph.set_defaults(handler=_graph)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"touchstone: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG, the same code launchd uses for a job it cannot start


if __name__ == "__main__":
    raise SystemExit(main())
