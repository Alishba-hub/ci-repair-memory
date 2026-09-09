from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents import design
from ci_memory_agents.dashboard_state import is_edited
from ci_memory_agents.prompt_builder import CONDITIONS
from ci_memory_agents.response_parser import parse_files, write_files

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_experiment import project_of

REPO_ROOT = Path(__file__).resolve().parents[1]

PRESETS = {
    "claude": {
        # The prompt goes over stdin: prompts reach 27K characters, which is close to
        # the Windows command-line limit and would be truncated as an argument.
        "command": ["claude", "-p", "--permission-mode", "acceptEdits"],
        "mode": "inplace",
        "stdin": True,
        "help": "Claude Code CLI, edits the workspace directly",
    },
    "cursor": {
        "command": ["cursor-agent", "-p", "{prompt}", "--force"],
        "mode": "inplace",
        "help": "Cursor CLI agent, edits the workspace directly",
    },
    "aider": {
        "command": ["aider", "--yes", "--no-auto-commits", "--message", "{prompt}"],
        "mode": "inplace",
        "help": "Aider, edits the workspace directly",
    },
}


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill one agent process and its descendants, by PID only.

    Never kill by image name: the developer's own editor and assistant sessions run
    the same executable, and a name based kill would take those down too.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def pending_runs(
    runs_root: Path,
    tasks_root: Path,
    agent: str,
    task_filter: str | None,
    project_filter: list[str] | None = None,
) -> tuple[list[Path], int]:
    """Runs with no agent response yet.

    A workspace that already differs from `repo_before` is skipped even without
    `agent_meta.json`: that is a run someone did by hand, and overwriting it would
    destroy real data.
    """
    agent_root = runs_root / agent
    if not agent_root.exists():
        raise SystemExit(f"No runs folder at {agent_root}. Generate prompts first.")
    pending: list[Path] = []
    protected = 0
    wanted_tasks = (
        {t.strip() for t in task_filter.split(",") if t.strip()} if task_filter else None
    )
    wanted_projects = {p.strip().lower() for p in (project_filter or []) if p.strip()} or None
    for task_dir in sorted(agent_root.iterdir()):
        if wanted_tasks and task_dir.name not in wanted_tasks:
            continue
        if wanted_projects and project_of(task_dir.name).lower() not in wanted_projects:
            continue
        repo_before = tasks_root / task_dir.name / "repo_before"
        for condition in CONDITIONS:
            condition_dir = task_dir / condition
            if not condition_dir.exists():
                continue
            for run_dir in sorted(condition_dir.iterdir()):
                if not (run_dir / "prompt.md").exists():
                    continue
                if (run_dir / "agent_meta.json").exists():
                    continue
                if repo_before.exists() and is_edited(run_dir / "workspace", repo_before):
                    protected += 1
                    continue
                pending.append(run_dir)

    # Order so that any prefix is a balanced sample. Folder order would spend a small
    # --limit entirely on one task in one condition, which cannot answer the research
    # question; run index, then task, then condition puts each task's two conditions
    # next to each other, so even --limit 2 yields one complete matched pair.
    pending.sort(key=lambda p: (p.name, p.parent.parent.name, p.parent.name))
    return pending, protected


def run_one(
    run_dir: Path, command: list[str], mode: str, timeout: int, use_stdin: bool = False,
    model: str = ""
) -> dict:
    prompt = (run_dir / "prompt.md").read_text(encoding="utf-8")
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    prompt_file = run_dir / "prompt_for_agent.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    resolved = [
        part.replace("{prompt}", prompt).replace("{prompt_file}", str(prompt_file))
        for part in command
    ]

    started = time.time()
    process = subprocess.Popen(
        resolved,
        cwd=workspace,
        stdin=subprocess.PIPE if use_stdin else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    try:
        stdout, stderr = process.communicate(input=prompt if use_stdin else None, timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        # Killing only the direct child leaves the agent's own subprocesses running.
        # Each timeout then leaks a process tree, which is what stalled an earlier batch.
        _kill_tree(process)
        stdout, stderr = process.communicate()
        returncode = -1
        (run_dir / "agent_timeout.txt").write_text(
            f"killed after {timeout}s\n", encoding="utf-8"
        )
    duration = time.time() - started

    reply = stdout or ""
    (run_dir / "agent_response.md").write_text(reply, encoding="utf-8")
    if stderr:
        (run_dir / "agent_stderr.txt").write_text(stderr, encoding="utf-8")
    result = type("Result", (), {"returncode": returncode})()

    written: list[str] = []
    if mode == "text":
        # The paths the task offers, and their original sizes, so the parser can fall
        # back to fenced blocks when a model ignores the `=== path ===` format -- and can
        # still tell a whole file from a quoted excerpt.
        baseline = run_dir.parents[2].name
        repo_before = REPO_ROOT / "tasks" / run_dir.parts[-4] / "repo_before"
        known, sizes = [], {}
        if repo_before.is_dir():
            for path in repo_before.rglob("*"):
                if path.is_file() and ".git" not in path.parts:
                    rel = path.relative_to(repo_before).as_posix()
                    known.append(rel)
                    sizes[rel] = len(path.read_text(encoding="utf-8", errors="replace"))
        written = write_files(workspace, parse_files(reply, known=sorted(known), sizes=sizes))

    prompt_file.unlink(missing_ok=True)
    return {
        "command": resolved[0],
        "mode": mode,
        # Which binary produced this run. Recorded because the folder name is a label,
        # not evidence: a runner invoked with the wrong agent writes perfectly ordinary
        # results into a folder named for a different one, and nothing downstream can
        # tell unless the run says what made it. That happened once already.
        "model_id": Path(str(command[0])).stem if command else "unknown",
        # The LLM behind the harness, which `model_id` does not capture: `claude.exe`
        # is the scaffold, not the model, and two runs of the same binary against
        # different models are two different conditions. Blank when the operator did
        # not say, which the CSV shows as an empty column rather than by guessing.
        "model": model,
        "exit_code": result.returncode,
        "prompt_chars": len(prompt),
        "reply_chars": len(reply),
        "files_written": written,
        "duration_ms": int(duration * 1000),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run every pending experiment run through a command line agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Presets:\n"
        + "\n".join(f"  {name:<8} {spec['help']}" for name, spec in PRESETS.items())
        + "\n\nCopilot is not a CLI tool. Automate it with the VS Code extension in\n"
        "vscode-extension/ instead.",
    )
    parser.add_argument("--preset", choices=sorted(PRESETS), help="Known agent CLI")
    parser.add_argument("--command", help="Custom command; use {prompt} or {prompt_file}")
    parser.add_argument(
        "--mode",
        choices=("inplace", "text"),
        help="inplace: the agent edits the workspace. text: parse its stdout for '=== path ===' blocks.",
    )
    parser.add_argument("--agent", default=design.AGENT_NAMES[0], help="runs/<agent> folder to fill")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "LLM behind the harness, recorded in each run's agent_meta.json. Defaults to "
            "whatever ci_memory_agents.design registers for --agent, so the registered "
            "cells need no flag."
        ),
    )
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--task-id", default=None, help="One task id, or a comma-separated list")
    parser.add_argument(
        "--project",
        action="append",
        default=None,
        help="Only tasks from this project, e.g. --project agno. Repeatable.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Stop after N runs, 0 = all")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per run")
    parser.add_argument("--delay", type=float, default=2.0, help="Pause between runs")
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Run this many agents at once. Runs are independent, so this scales almost linearly.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List what would run, then stop")
    parser.add_argument("--stdin", action="store_true", help="Send the prompt on stdin")
    parser.add_argument("--exe", default=None, help="Full path to the agent binary, overriding the preset")
    args = parser.parse_args()

    if args.preset:
        spec = PRESETS[args.preset]
        command = list(spec["command"])
        mode = args.mode or spec["mode"]
        use_stdin = args.stdin or spec.get("stdin", False)
    elif args.command:
        import shlex

        command = shlex.split(args.command)
        mode = args.mode or "text"
        use_stdin = args.stdin
    else:
        parser.error("give either --preset or --command")

    if args.exe:
        command[0] = args.exe

    # Explicit flag first, then whatever the design registers for this agent name. An
    # unregistered agent with no --model records an empty model rather than inheriting
    # another cell's, because a wrong attribution is worse than a missing one.
    model = args.model if args.model is not None else design.agent_config(args.agent)["model"]
    if model:
        print(f"Recording model: {model}")
    else:
        print(
            f"No model recorded for {args.agent!r}. Pass --model so the runs can be "
            f"attributed to an LLM, or add the cell to ci_memory_agents.design.AGENTS."
        )

    pending, protected = pending_runs(
        Path(args.runs_root), Path(args.tasks_root), args.agent, args.task_id, args.project
    )
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print("Nothing pending. Every run already has an agent response.")
        if protected:
            print(f"({protected} run(s) already edited by hand were left untouched.)")
        return 0

    print(f"Agent command : {' '.join(command[:2])} ...")
    print(f"Mode          : {mode}")
    print(f"Runs pending  : {len(pending)}")
    if protected:
        print(f"Skipping      : {protected} run(s) already edited by hand")
    print()
    if args.dry_run:
        for run_dir in pending[:20]:
            print(f"  would run {run_dir.relative_to(Path(args.runs_root))}")
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
        return 0

    completed = failed = 0

    def execute(item):
        index, run_dir = item
        label = "/".join(run_dir.parts[-3:])
        try:
            meta = run_one(run_dir, command, mode, args.timeout, use_stdin, model=model)
            (run_dir / "agent_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            detail = ", ".join(meta["files_written"]) or "agent edited in place"
            return True, f"[{index}/{len(pending)}] ok   {label} ({meta['duration_ms'] // 1000}s) {detail}"
        except FileNotFoundError:
            return False, f"[{index}/{len(pending)}] FAILED {label}: '{command[0]}' not found on PATH"
        except Exception as error:
            return False, f"[{index}/{len(pending)}] FAILED {label}: {type(error).__name__}: {error}"

    items = list(enumerate(pending, start=1))
    if args.parallel > 1:
        from concurrent.futures import ThreadPoolExecutor

        print(f"Running {args.parallel} at a time\n")
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            for ok, line in pool.map(execute, items):
                completed += ok
                failed += not ok
                print(line, flush=True)
    else:
        for item in items:
            ok, line = execute(item)
            completed += ok
            failed += not ok
            print(line, flush=True)
            time.sleep(args.delay)

    print(f"\nDone. {completed} succeeded, {failed} failed.")
    print(f"Score them with:  python scripts/run_experiment.py --mode score --agent {args.agent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
