from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(label: str, command: list[str]) -> None:
    print(f"\n== {label} ==")
    print(" ".join(shlex.quote(part) for part in command))
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _python(script: str, *args: str) -> list[str]:
    return [sys.executable, str(REPO_ROOT / script), *args]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the CI memory experiment end-to-end with one command",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--agent", default="claude-code", help="runs/<agent> folder to fill")
    parser.add_argument("--preset", default="claude", help="CLI preset for scripts/auto_run.py")
    parser.add_argument("--command", default=None, help="Custom CLI command for the agent")
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--source", default=None, help="Filter tasks by source")
    parser.add_argument("--runs", type=int, default=10, help="Repeated runs per condition")
    parser.add_argument("--parallel", type=int, default=6, help="How many agent runs to launch at once")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds per agent run")
    parser.add_argument("--delay", type=float, default=2.0, help="Pause between agent runs")
    parser.add_argument(
        "--output-mode",
        choices=("text", "inplace"),
        default="inplace",
        help="Prompt format passed to scripts/run_experiment.py",
    )
    parser.add_argument("--stdin", action="store_true", help="Send the prompt on stdin to the agent CLI")
    parser.add_argument("--exe", default=None, help="Full path to the agent binary")
    parser.add_argument("--skip-score", action="store_true", help="Do not run the final textual score step")
    args = parser.parse_args()

    common = [
        "--tasks-root",
        args.tasks_root,
        "--runs-root",
        args.runs_root,
        "--agent",
        args.agent,
    ]
    if args.task_id:
        common += ["--task-id", args.task_id]
    if args.source:
        common += ["--source", args.source]

    _run(
        "Generate prompts",
        _python(
            "scripts/run_experiment.py",
            "--mode",
            "prompts",
            *common,
            "--runs",
            str(args.runs),
            "--output-mode",
            args.output_mode,
        ),
    )

    auto_command = [
        "--agent",
        args.agent,
        "--runs-root",
        args.runs_root,
        "--tasks-root",
        args.tasks_root,
        "--preset",
        args.preset,
        "--parallel",
        str(args.parallel),
        "--timeout",
        str(args.timeout),
        "--delay",
        str(args.delay),
    ]
    if args.task_id:
        auto_command += ["--task-id", args.task_id]
    if args.stdin:
        auto_command.append("--stdin")
    if args.command:
        auto_command = [
            "--agent",
            args.agent,
            "--runs-root",
            args.runs_root,
            "--tasks-root",
            args.tasks_root,
            "--command",
            args.command,
            "--parallel",
            str(args.parallel),
            "--timeout",
            str(args.timeout),
            "--delay",
            str(args.delay),
        ]
        if args.task_id:
            auto_command += ["--task-id", args.task_id]
        if args.stdin:
            auto_command.append("--stdin")

    _run("Run agent", _python("scripts/auto_run.py", *auto_command, "--exe", args.exe) if args.exe else _python("scripts/auto_run.py", *auto_command))

    if not args.skip_score:
        _run(
            "Score textual similarity diagnostics",
            _python(
                "scripts/run_experiment.py",
                "--mode",
                "score",
                *common,
                "--k",
                "1",
            ),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())