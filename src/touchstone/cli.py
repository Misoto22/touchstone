"""The command line.

The stable command surface for initialization, diagnostics, scheduled runs,
human decisions, and graph documentation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from touchstone import visualise
from touchstone.config import ConfigError, load


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
    if args.non_interactive:
        if not engine or not model or not workflows or not schedule:
            raise ConfigError(
                "non-interactive init requires --engine, --model, --schedule, "
                "and at least one --workflow"
            )
    else:
        engine = engine or input("Engine (codex or claude) [codex]: ").strip() or "codex"
        model = model or input("Model: ").strip()
        if not workflows:
            workflow = input("Required workflow [ci.yml]: ").strip() or "ci.yml"
            workflows = (workflow,)
        schedule = schedule or input("Schedule [hourly@00]: ").strip() or "hourly@00"
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
    failed = any(fields["failed"] or fields["inconclusive"] for fields in report.values())
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


def _run(args: argparse.Namespace) -> int:
    from touchstone.runner import execute

    config = load(args.config)
    print(f"touchstone: {config.describe()}", file=sys.stderr)
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
    migrate = config_sub.add_parser("migrate", help="migrate an unversioned config")
    migrate.add_argument("path", type=Path)
    migrate.set_defaults(handler=_migrate_config)

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
