from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents import design
from ci_memory_agents.evaluator import evaluate_submission, pass_at_k
from ci_memory_agents.loader import list_tasks, study_task_ids
from ci_memory_agents.prompt_builder import (
    ALL_CONDITIONS,
    CONDITIONS,
    NO_MEMORY,
    PAIR,
    build_prompt,
    condition_k,
    condition_label,
)


def _is_stale(workspace: Path, repo_before: Path) -> bool:
    """Does this untouched workspace still match the task's current `repo_before`?"""
    def tree(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)).replace("\\", "/"): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

    return tree(workspace) != tree(repo_before)


def command_prompts(args, tasks_root: Path, runs_root: Path) -> int:
    tasks = _select(tasks_root, args)
    refreshed: list[Path] = []

    # The design's arms by default. `--conditions` exists so the placebo arm can be laid
    # out at all: `foreign_memory` is defined in prompt_builder and was reachable from
    # no command, which left the length confound -- memory prompts are longer than the
    # control's, so an effect could be context rather than memory -- with no way to
    # measure it. It is deliberately not in CONDITIONS; asking for it is explicit.
    conditions = CONDITIONS
    if getattr(args, "conditions", ""):
        wanted = [c.strip() for c in args.conditions.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in ALL_CONDITIONS]
        if unknown:
            raise SystemExit(
                f"unknown condition(s) {', '.join(unknown)}. "
                f"Known: {', '.join(ALL_CONDITIONS)}"
            )
        conditions = tuple(wanted)

    # A task that cannot fill every arm is dropped from the layout entirely, not just
    # from the arms it cannot fill. Laying out its no_memory and memory_k1 cells would
    # put it in the denominator of some arms and not others, and every paired comparison
    # downstream -- RQ1's McNemar test, RQ3's K curve -- assumes the arms share one
    # population. Tasks imported before the K sweep carry three memory items and land
    # here; re-import them with scripts/import_ci_repair_bench.py.
    needed = max(condition_k(c) for c in conditions)
    short = [task for task in tasks if not task.supports(needed)]
    tasks = [task for task in tasks if task.supports(needed)]
    if short:
        print(f"Skipping {len(short)} task(s) with fewer than {needed} memory items:")
        for task in short:
            print(f"  {task.task_id}: has {task.memory_size}")
        print("  Re-import them with: python scripts/import_ci_repair_bench.py\n")
    if not tasks:
        print("No task can fill every arm. Nothing laid out.")
        return 1

    for task in tasks:
        for condition in conditions:
            for run in range(1, args.runs + 1):
                run_dir = runs_root / args.agent / task.task_id / condition / f"run_{run:02d}"
                run_dir.mkdir(parents=True, exist_ok=True)
                # The prompt of a cell an agent has run is part of its result: it is what
                # that agent was actually given. Rewriting it after a format change would
                # make the record claim an input the run never saw.
                if not (run_dir / "agent_meta.json").exists():
                    (run_dir / "prompt.md").write_text(
                        build_prompt(task, condition, output_mode=args.output_mode),
                        encoding="utf-8",
                    )
                workspace = run_dir / "workspace"
                # A workspace is rebuilt only when it is missing, or when it is stale and
                # nothing has been run against it. `agent_meta.json` is the line: a cell
                # an agent has touched is somebody's result and is never overwritten,
                # which is the same protection auto_run relies on. An untouched but stale
                # workspace is the dangerous case -- `materialize_repos.py` can replace a
                # task's `repo_before` with a full checkout long after the cells were
                # laid out, and an agent handed the old gold-files-only copy would be run
                # under a different scope from its siblings without anything saying so.
                if workspace.exists() and args.refresh_workspaces:
                    if not (run_dir / "agent_meta.json").exists() and _is_stale(
                        workspace, task.repo_before
                    ):
                        shutil.rmtree(workspace)
                        refreshed.append(run_dir)
                if not workspace.exists():
                    # `.git` is excluded deliberately. It is copied once per run, so for
                    # a real checkout it is most of the bytes. It also removes any way
                    # for history to become an input: the depth-1 checkout carries only
                    # the failing commit today, but a future deeper one would carry the
                    # commit that fixed the build, which is the answer.
                    # CI-Repair-Bench bans .git access for the same reason.
                    shutil.copytree(
                        task.repo_before, workspace, ignore=shutil.ignore_patterns(".git")
                    )
    total = len(tasks) * len(conditions) * args.runs
    print(f"Wrote {total} prompts for {len(tasks)} tasks x {len(conditions)} conditions x {args.runs} runs")
    print(f"Conditions: {', '.join(conditions)}")
    if refreshed:
        print(f"Rebuilt {len(refreshed)} stale workspaces that no agent had run against.")
    elif args.refresh_workspaces:
        print("No stale workspaces; every untouched copy already matches its repo_before.")
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

    # `ALL_CONDITIONS`, not `CONDITIONS`: arms collected before the K sweep still hold
    # real runs, and a scorer that enumerates only the current design would drop them
    # without saying so. That exact bug once removed a whole arm from the results.
    config = design.agent_config(args.agent)
    results = []
    for task_dir in sorted(agent_root.iterdir()):
        task = tasks.get(task_dir.name)
        if task is None:
            continue
        for condition in ALL_CONDITIONS:
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
                # Harness and model travel separately. "copilot" alone stopped
                # identifying a condition once the same harness was driven by more than
                # one model, and two models' results would otherwise pool into a single
                # row with nothing left in the record to separate them again.
                payload["harness"] = config["harness"]
                payload["model"] = config["model"]
                payload["k"] = condition_k(condition)
                payload["repo"] = task.repo_name
                payload["error_type"] = task.error_type
                payload["error_group"] = task.error_group
                payload["memory_available"] = task.memory_size
                results.append(payload)

    if not results:
        print("No completed workspaces to score.")
        return 1

    results_path = runs_root / f"results_{args.agent}.jsonl"
    with results_path.open("w", encoding="utf-8") as handle:
        for payload in results:
            handle.write(json.dumps(payload) + "\n")
    print(f"Wrote {len(results)} results to {results_path}")

    # The CSV is written on every scoring pass rather than on request. The point of
    # persisting results is that a later analysis need not re-run the agents, and an
    # export that has to be remembered is one that gets skipped on the run that mattered.
    csv_path = write_runs_csv(results, runs_root / "csv" / f"runs_{args.agent}.csv")
    print(f"Wrote {len(results)} rows to {csv_path}\n")
    _summarize(results, args.k)
    return 0


#: Columns of the per-run CSV, in order. One row per (agent, task, condition, run) --
#: the long format every downstream analysis wants, and the level at which RQ2's
#: run-to-run variance is still visible. Aggregating before writing would make
#: consistency across repeats unrecoverable from the file.
CSV_COLUMNS = (
    "agent",
    "harness",
    "model",
    "task_id",
    "repo",
    "error_group",
    "error_type",
    "condition",
    "k",
    "run",
    "solved",
    "exact_match",
    "normalized_match",
    "localized",
    "touched_any_gold",
    "files_matched",
    "files_expected",
    "file_recall",
    "file_precision",
    "file_f1",
    "file_iou",
    "line_deviation_ratio",
    "memory_available",
)


def write_runs_csv(results: list[dict], path: Path) -> Path:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for payload in sorted(results, key=lambda r: (r["task_id"], r["condition"], r["run"])):
            writer.writerow([_csv_cell(payload.get(name)) for name in CSV_COLUMNS])
    return path


def _csv_cell(value):
    """Booleans as 0/1, lists pipe-joined, so pandas and R read the column as-is."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return "|".join(str(item) for item in value)
    return "" if value is None else value


def _summarize(results: list[dict], k: int) -> None:
    by_condition: dict[str, list[dict]] = defaultdict(list)
    for payload in results:
        by_condition[payload["condition"]].append(payload)

    # Ordered by K rather than alphabetically, so the table reads as the dose-response
    # curve RQ3 asks for instead of putting memory_k1 next to memory_k3 next to
    # memory_k5 next to no_memory in an order that means nothing.
    ordered = sorted(by_condition, key=lambda c: (condition_k(c), c))

    print(f"{'condition':<14}{'runs':>6}{'exact':>8}{'norm':>8}{'IoU':>8}{'recall':>8}{'linedev':>9}")
    for condition in ordered:
        payloads = by_condition[condition]
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
    for condition in sorted(scores, key=lambda c: (condition_k(c), c)):
        values = scores[condition]
        print(
            f"  {condition:<14}{statistics.mean(values):.3f}  over {len(values)} tasks"
            f"   ({condition_label(condition)})"
        )

    # RQ1: one named contrast, always the same two arms. Taking whichever two arms
    # happen to be present -- which is what `len(scores) == 2` used to do -- reports a
    # different comparison depending on how much of the grid has been filled, and would
    # silently start calling K=1 vs K=5 "the memory effect" once four arms existed.
    control, memory_arm = PAIR
    if control in scores and memory_arm in scores:
        delta = statistics.mean(scores[memory_arm]) - statistics.mean(scores[control])
        print(f"\n  RQ1 memory effect ({control} -> {memory_arm}): {delta:+.3f}")

    # RQ3: the same number at every K, so the reader can see whether more history helps
    # monotonically, plateaus, or reverses.
    curve = [
        (condition_k(c), statistics.mean(scores[c]))
        for c in scores
        if condition_k(c) and c in CONDITIONS
    ]
    if len(curve) > 1:
        print("\n  RQ3 memory size curve (Pass@%d vs K)" % k)
        baseline = statistics.mean(scores[control]) if control in scores else None
        for size, value in sorted(curve):
            against = f"  {value - baseline:+.3f} vs K=0" if baseline is not None else ""
            print(f"    K={size:<3}{value:.3f}{against}")


def _rate(payloads: list[dict], key: str) -> float:
    return sum(1 for p in payloads if p[key]) / len(payloads)


def project_of(task_id: str) -> str:
    """`crb_<project>_<instance>` -> `<project>`.

    Split from the right, because project names contain underscores and hyphens
    (`django-import-export`, `openai-python`) while the instance is always digits.
    """
    stem = task_id[4:] if task_id.startswith("crb_") else task_id
    head, _, tail = stem.rpartition("_")
    return head if tail.isdigit() and head else stem


def select_tasks(tasks_root: Path, source=None, task_id=None, projects=None, limit=0):
    """The tasks to act on, narrowed by whatever the caller asked for.

    Every entry point narrows the same way, so `--project agno --tasks 4` means the
    same thing when laying out cells, running the agent and scoring. A filter that
    matched different sets in different steps would quietly compare one population
    against another.
    """
    tasks = list_tasks(tasks_root, source=source)
    # The declared design population. An explicit --task-id still reaches a leftover
    # from an earlier design, so old runs stay inspectable.
    if not task_id:
        declared = study_task_ids(tasks_root)
        if declared is not None:
            tasks = [task for task in tasks if task.task_id in declared]
    if task_id:
        wanted = {t.strip() for t in task_id.split(",") if t.strip()}
        tasks = [task for task in tasks if task.task_id in wanted]
        missing = wanted - {task.task_id for task in tasks}
        if missing:
            raise SystemExit(f"no task named {', '.join(sorted(missing))}")
    if projects:
        wanted = {p.strip().lower() for p in projects if p.strip()}
        tasks = [task for task in tasks if project_of(task.task_id).lower() in wanted]
        if not tasks:
            known = sorted({project_of(t.task_id) for t in list_tasks(tasks_root, source=source)})
            raise SystemExit(
                f"no tasks for project {', '.join(sorted(wanted))}.\n"
                f"Available: {', '.join(known)}"
            )
    if limit:
        # One task per project first, then a second from each, and so on. Taking the
        # first N in order would spend a small --tasks entirely on whichever project
        # happens to sort first, and a study of one project is not the study.
        by_project: dict[str, list] = {}
        for task in tasks:
            by_project.setdefault(project_of(task.task_id), []).append(task)
        interleaved = []
        for depth in range(max((len(v) for v in by_project.values()), default=0)):
            for project in sorted(by_project):
                if depth < len(by_project[project]):
                    interleaved.append(by_project[project][depth])
        tasks = interleaved[:limit]
    return tasks


def _select(tasks_root: Path, args):
    return select_tasks(
        tasks_root,
        source=args.source,
        task_id=args.task_id,
        projects=getattr(args, "project", None),
        limit=getattr(args, "tasks", 0),
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run the CI memory agents experiment")
    parser.add_argument("--mode", choices=("prompts", "score"), default="prompts")
    parser.add_argument(
        "--conditions",
        default="",
        help=(
            "comma-separated arms to lay out instead of the design's. The only "
            "supported use is the placebo arm, foreign_memory; narrowing the design "
            "arms here lays out a task in some arms and not others, which breaks "
            "every paired comparison downstream."
        ),
    )
    parser.add_argument("--tasks-root", default=str(repo_root / "tasks"))
    parser.add_argument("--runs-root", default=str(repo_root / "runs"))
    parser.add_argument(
        "--agent",
        default=design.AGENT_NAMES[0],
        help=(
            "Agent cell under evaluation. Registered cells: "
            + ", ".join(design.AGENT_NAMES)
            + ". Any other name works too but carries no model attribution."
        ),
    )
    parser.add_argument("--task-id", default=None, help="One task id, or a comma-separated list")
    parser.add_argument(
        "--project",
        action="append",
        default=None,
        help="Only tasks from this project, e.g. --project agno. Repeatable.",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=0,
        help="Use at most this many tasks, spread across projects rather than taken in order",
    )
    parser.add_argument("--source", default=None, help="Filter tasks by source, e.g. ci-repair-bench")
    parser.add_argument(
        "--runs",
        type=int,
        default=design.RUNS_PER_CONDITION,
        help=(
            f"Repeated runs per condition (RQ2). Default {design.RUNS_PER_CONDITION}, "
            "from ci_memory_agents.design"
        ),
    )
    parser.add_argument("--k", type=int, default=1, help="k for Pass@K")
    parser.add_argument(
        "--refresh-workspaces",
        action="store_true",
        help=(
            "Rebuild workspaces that no longer match the task's repo_before, but only in "
            "cells where no agent has run. Use after materialize_repos.py, which can "
            "change a task's scope after its cells were laid out. Cells with an "
            "agent_meta.json are never touched."
        ),
    )
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
