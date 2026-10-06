"""Run one task's arm through GitHub Copilot, started and watched from a command window.

Copilot has no command-line agent, so this does not call Copilot itself. It hands the
request to the VS Code extension (vscode-extension/, v0.4.1 or newer) through a
vscode:// link, then follows the progress file the extension writes and prints each
line, so a run can be started from cmd -- or from the dashboard's run buttons, which
open this script in a new window -- and watched to the end.

    python scripts/run_copilot.py --agent copilot-gpt-5.4 --task-id crb_agno_129 --condition memory_k3

Only the task's pending runs in that arm are sent: a run with agent_meta.json is done,
and the extension skips it. VS Code must be open and signed in to Copilot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ci_memory_agents import design
from ci_memory_agents.prompt_builder import CONDITIONS, condition_label

EXTENSION_ID = "ci-memory-agents.ci-memory-agents-runner"
BATCHES_DIR = REPO_ROOT / ".ci_batches"
#: How long VS Code gets to pick the link up before the window says something is wrong.
PICKUP_TIMEOUT_S = 90
#: A batch whose progress file has not changed for this long is treated as abandoned,
#: typically because VS Code was closed. A single Copilot request rarely takes minutes.
STALE_AFTER_S = 900


def open_link(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)  # the vscode:// handler VS Code registers at install
    else:
        webbrowser.open(url)


def arm_state(runs_root: Path, agent: str, task_id: str, condition: str) -> tuple[int, int, int]:
    """(runs, done, failed last time) for one task's arm."""
    runs = [p for p in sorted((runs_root / agent / task_id / condition).glob("run_*")) if p.is_dir()]
    done = sum((run / "agent_meta.json").exists() for run in runs)
    errored = sum(
        not (run / "agent_meta.json").exists() and (run / "agent_error.json").exists() for run in runs
    )
    return len(runs), done, errored


def follow(status_path: Path) -> int:
    started = time.time()
    shown = 0
    while True:
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            status = None

        if status is None:
            if time.time() - started > PICKUP_TIMEOUT_S:
                print(
                    f"\nVS Code did not pick up the request within {PICKUP_TIMEOUT_S}s. Check that:\n"
                    "  - VS Code is open (the first time, it asks whether to open the link: choose Open)\n"
                    "  - the CI Memory Agents Runner extension is v0.4.1 or newer\n"
                    "    ('CI Memory: List Available Copilot Models' prints the version)\n"
                    "  - you are signed in to GitHub Copilot"
                )
                return 1
            time.sleep(2)
            continue

        for line in status.get("log", [])[shown:]:
            print(line, flush=True)
        shown = len(status.get("log", []))

        state = status.get("state")
        if state != "running":
            print(f"\n{state.upper()}: {status.get('message', '')}")
            return 0 if state == "done" and not status.get("failed") else 1

        updated = status.get("updated_at", "")
        try:
            age = time.time() - status_path.stat().st_mtime
        except OSError:
            age = 0
        if age > STALE_AFTER_S:
            print(
                f"\nNo progress for {int(age // 60)} minutes (last update {updated}). VS Code was "
                "probably closed. Runs that finished are kept; start this arm again to continue."
            )
            return 1
        time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one task's arm through Copilot via the VS Code extension")
    parser.add_argument("--agent", required=True, choices=sorted(design.RUNNER_AGENTS))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--condition", required=True, choices=CONDITIONS)
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    args = parser.parse_args()

    runs_root = Path(args.runs_root)
    total, done, errored = arm_state(runs_root, args.agent, args.task_id, args.condition)
    print("=" * 64)
    print(f"  Copilot run: {args.task_id} / {condition_label(args.condition)}")
    print("=" * 64)
    print(f"  Agent  : {args.agent}  (model {design.RUNNER_AGENTS[args.agent]})")
    print(f"  Runs   : {total}   done {done}   pending {total - done}"
          + (f"   ({errored} failed last time and will be retried)" if errored else ""))
    if not total:
        print(
            "\nNo run folders for this arm. Lay out the prompts first:\n"
            f"  python scripts/run_experiment.py --mode prompts --agent {args.agent}"
        )
        return 2
    if done == total:
        print("\nEvery run of this arm already has a response. Nothing to do.")
        return 0

    request = uuid.uuid4().hex[:12]
    url = f"vscode://{EXTENSION_ID}/run?" + urlencode(
        {"agent": args.agent, "task": args.task_id, "condition": args.condition, "request": request}
    )
    print(f"\n  Handing the request to VS Code. Keep VS Code open; Copilot runs there.\n  {url}\n")
    open_link(url)
    return follow(BATCHES_DIR / f"{request}.json")


if __name__ == "__main__":
    code = main()
    print("\nYou can close this window.")
    raise SystemExit(code)
