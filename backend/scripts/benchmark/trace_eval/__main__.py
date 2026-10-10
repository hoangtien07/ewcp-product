"""CLI for the trace_eval acceptance evaluator.

``run --traces <jsonl> [--output results.json]`` grades each canned trace
and writes the spec-schema result rows + a summary. ``--output`` defaults
to stdout when omitted. Exits non-zero when any
``evaluator_assertions_passed`` is false — the evaluator grading its own
oracle wrong is the failure worth failing loudly for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evaluator import evaluate_cases, load_cases, summarize

DEFAULT_TRACES = Path(__file__).parent / "fixtures" / "cases.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trace_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="grade a JSONL trace file")
    run.add_argument(
        "--traces",
        type=Path,
        default=DEFAULT_TRACES,
        help="JSONL case file (default: committed fixtures)",
    )
    run.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write JSON results here (default: stdout)",
    )
    return parser


def cmd_run(traces: Path, output: Path | None) -> int:
    cases = load_cases(traces)
    results = evaluate_cases(cases)
    payload = {"cases": results, "summary": summarize(results)}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["summary"]["evaluator_assertions_passed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args.traces, args.output)
    return 2


if __name__ == "__main__":
    sys.exit(main())
