from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents.evaluator import evaluate_submission, pass_at_k
from ci_memory_agents.loader import list_tasks
from ci_memory_agents.prompt_builder import CONDITIONS, build_prompt


def command_prompts(args, tasks_root: Path, runs_root: Path) -> int:
    tasks = _select(tasks_root, args)
    for task in tasks:
        for condition in CONDITIONS:
            for run in range(1, args.runs + 1):
                run_dir = runs_root / args.agent / task.task_id / condition / f"run_{run:02d}"
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "prompt.md").write_text(
                    build_prompt(task, condition, output_mode=args.output_mode),
                    encoding="utf-8",
                )
                workspace = run_dir / "workspace"
                if not workspace.exists():
                    shutil.copytree(task.repo_before, workspace)
    total = len(tasks) * len(CONDITIONS) * args.runs
    print(f"Wrote {total} prompts for {len(tasks)} tasks x {len(CONDITIONS)} conditions x {args.runs} runs")
    print(f"Root: {runs_root / args.agent}")
    print("\nFor each run: open the workspace in your agent, paste prompt.md, let it edit the")
    print("workspace in place, then score with --mode score.")
    return 0


def command_score(args, tasks_root: Path, runs_root: Path) -> int:
    tasks = {task.task_id: task for task in _select(tasks_root, args)}
    agent_root = runs_root / args.agent
    if not agent_root.exists():
        print(f"No runs found at {agent_root}. Generate prompts first.")
        return 1

    results = []
    for task_dir in sorted(agent_root.iterdir()):
        task = tasks.get(task_dir.name)
        if task is None:
            continue
        for condition in CONDITIONS:
            condition_dir = task_dir / condition
            if not condition_dir.exists():
                continue
            for run_dir in sorted(condition_dir.iterdir()):
                workspace = run_dir / "workspace"
                if not workspace.exists():
                    continue
                result = evaluate_submission(
                    workspace, task.repo_before, task.repo_after, task.task_id, condition
                )
                payload = result.as_dict()
                payload["run"] = run_dir.name
                payload["agent"] = args.agent
                payload["error_type"] = task.error_type
                results.append(payload)

    if not results:
        print("No completed workspaces to score.")
        return 1

    results_path = runs_root / f"results_{args.agent}.jsonl"
    with results_path.open("w", encoding="utf-8") as handle:
        for payload in results:
            handle.write(json.dumps(payload) + "\n")
    print(f"Wrote {len(results)} results to {results_path}\n")
    _summarize(results, args.k)
    return 0


def _summarize(results: list[dict], k: int) -> None:
    by_condition: dict[str, list[dict]] = defaultdict(list)
    for payload in results:
        by_condition[payload["condition"]].append(payload)

    print(f"{'condition':<14}{'runs':>6}{'exact':>8}{'norm':>8}{'IoU':>8}{'recall':>8}{'linedev':>9}")
    for condition, payloads in sorted(by_condition.items()):
        print(
            f"{condition:<14}{len(payloads):>6}"
            f"{_rate(payloads, 'exact_match'):>8.2f}"
            f"{_rate(payloads, 'normalized_match'):>8.2f}"
            f"{statistics.mean(p['file_iou'] for p in payloads):>8.2f}"
            f"{statistics.mean(p['file_recall'] for p in payloads):>8.2f}"
            f"{statistics.median(p['line_deviation_ratio'] for p in payloads):>9.2f}"
        )

    print(f"\nPass@{k} (success = normalized_match)")
    by_task: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for payload in results:
        by_task[(payload["task_id"], payload["condition"])].append(payload)
    scores: dict[str, list[float]] = defaultdict(list)
    for (_, condition), payloads in by_task.items():
        total = len(payloads)
        correct = sum(1 for p in payloads if p["normalized_match"])
        if total >= k:
            scores[condition].append(pass_at_k(total, correct, k))
    for condition, values in sorted(scores.items()):
        print(f"  {condition:<14}{statistics.mean(values):.3f}  over {len(values)} tasks")
    if len(scores) == 2:
        delta = statistics.mean(scores["with_memory"]) - statistics.mean(scores["no_memory"])
        print(f"\n  memory effect: {delta:+.3f}")


def _rate(payloads: list[dict], key: str) -> float:
    return sum(1 for p in payloads if p[key]) / len(payloads)


def _select(tasks_root: Path, args):
    tasks = list_tasks(tasks_root, source=args.source)
    if args.task_id:
        tasks = [task for task in tasks if task.task_id == args.task_id]
        if not tasks:
            raise SystemExit(f"no task named {args.task_id}")
    return tasks


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run the CI memory agents experiment")
    parser.add_argument("--mode", choices=("prompts", "score"), default="prompts")
    parser.add_argument("--tasks-root", default=str(repo_root / "tasks"))
    parser.add_argument("--runs-root", default=str(repo_root / "runs"))
    parser.add_argument("--agent", default="copilot", help="Agent under evaluation")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--source", default=None, help="Filter tasks by source, e.g. ci-repair-bench")
    parser.add_argument("--runs", type=int, default=10, help="Repeated runs per condition")
    parser.add_argument("--k", type=int, default=1, help="k for Pass@K")
    parser.add_argument(
        "--output-mode",
        choices=("text", "inplace"),
        default="text",
        help="text: the agent replies with file contents. inplace: the agent edits the workspace itself.",
    )
    args = parser.parse_args()

    tasks_root = Path(args.tasks_root).resolve()
    runs_root = Path(args.runs_root).resolve()
    if args.mode == "prompts":
        return command_prompts(args, tasks_root, runs_root)
    return command_score(args, tasks_root, runs_root)


if __name__ == "__main__":
    raise SystemExit(main())
