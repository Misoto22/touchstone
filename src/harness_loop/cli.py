"""The command line.

Three verbs. `run` is what a scheduler calls, `resume` is what a person calls
after answering a parked draft, and `graph` is how the picture stays honest.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness_loop import visualise
from harness_loop.config import ConfigError, load


def _run(args: argparse.Namespace) -> int:
    from harness_loop.runner import execute

    config = load(args.config)
    print(f"harness-loop: {config.describe()}", file=sys.stderr)
    return execute(config, loop=args.loop, dry_run=args.dry_run)


def _resume(args: argparse.Namespace) -> int:
    from harness_loop.runner import resume

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
    parser = argparse.ArgumentParser(prog="harness-loop", description=__doc__)
    parser.add_argument("--config", type=Path, help="a TOML file; discovered when omitted")
    sub = parser.add_subparsers(dest="command", required=True)

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
        print(f"harness-loop: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG, the same code launchd uses for a job it cannot start


if __name__ == "__main__":
    raise SystemExit(main())
