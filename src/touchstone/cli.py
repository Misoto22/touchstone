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

    engine = args.engine
    model = args.model
    workflows = tuple(args.workflow or ())
    schedule = args.schedule
    if args.non_interactive:
        if not engine or not model:
            raise ConfigError("non-interactive init requires --engine and --model")
    else:
        engine = engine or input("Engine (codex or claude) [codex]: ").strip() or "codex"
        model = model or input("Model: ").strip()
        if not workflows:
            workflow = input("Required workflow [ci.yml]: ").strip() or "ci.yml"
            workflows = (workflow,)
        schedule = schedule or input("Schedule [hourly]: ").strip() or "hourly"
    if engine not in ("codex", "claude"):
        raise ConfigError("engine must be 'codex' or 'claude'")
    path = initialize(
        InitOptions(
            start=args.path,
            engine=engine,
            model=model or "",
            workflows=workflows,
            schedule=schedule or "hourly",
            output=args.output,
            force=args.force,
        ),
        LocalExecutor(),
    )
    print(f"wrote {path}")
    return 0


def _run(args: argparse.Namespace) -> int:
    from touchstone.runner import execute

    config = load(args.config)
    print(f"touchstone: {config.describe()}", file=sys.stderr)
    return execute(config, loop=args.loop, dry_run=args.dry_run)


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
    init.add_argument("--schedule", help="hourly, daily@HH:MM, or weekly@DAY,HH:MM")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    init.set_defaults(handler=_init)

    run = sub.add_parser("run", help="one iteration of a loop")
    run.add_argument("loop", help="which loop, by its [loop.*] name")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="audit, classify and review for real; stop before publishing",
    )
    run.set_defaults(handler=_run)

    resume = sub.add_parser("resume", help="answer a parked draft and continue that thread")
    resume.add_argument("thread", help="the thread id the parked run reported")
    resume.add_argument("answer", choices=("merge", "close"))
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
