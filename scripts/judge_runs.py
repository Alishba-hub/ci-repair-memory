"""Judge completed runs on whether they actually repair the CI failure.

    python scripts\\judge_runs.py --agent claude-code --exe "<claude.exe>" --parallel 6
    python scripts\\judge_runs.py --agent claude-code --mode report
    python scripts\\judge_runs.py --agent claude-code --mode calibration --sample 20

Textual match against the maintainer's commit cannot answer whether a repair works;
see the header of `src/ci_memory_agents/judge.py` for the evidence. This scores every
run on the question the experiment asks, and caches each verdict in the run folder so
`--mode report` is free to re-run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ci_memory_agents.evaluator import pass_at_k
from ci_memory_agents.judge import (
    JUDGE_VERSION,
    judge_run,
    read_verdict,
    workspace_diff,
)
from ci_memory_agents.loader import list_tasks
from ci_memory_agents.prompt_builder import (
    ALL_CONDITIONS,
    CONDITIONS,
    PAIR,
    canonical_condition,
    condition_k,
)


def _safe(text: str) -> str:
    """Printable on this console.

    The judge writes prose, so a verdict can contain an arrow or a dash that the
    Windows console codepage cannot encode. Losing a whole batch to a print is not
    acceptable when each line cost a model call; the verdict file itself is UTF-8 and
    keeps the original characters.
    """
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _judgeable(run_dir: Path, repo_before: Path) -> bool:
    """Runs that need a model call to decide.

    A run killed by the timeout was interrupted mid-edit, so its files are a partial
    answer; judging it would score "ran out of time" as "got it wrong".

    A run that changed nothing is excluded here too, but for the opposite reason: it is
    already decided. `static_checks` calls it a failure exactly, and `oracle.resolve`
    keeps it in the denominator. That distinction is not cosmetic -- it is 70% of the
    runs in the pilot. The earlier version of this function excluded them from judging
    AND from every rate computed downstream, so the reported pass ratio was measured
    over the minority of runs where the agent produced a patch at all. An agent that
    emits nothing has failed to repair the build; it has not opted out of the
    experiment.
    """
    workspace = run_dir / "workspace"
    if not workspace.exists() or (run_dir / "agent_timeout.txt").exists():
        return False
    return workspace_diff(repo_before, workspace) != "[the agent changed no files]"


def collect_runs(runs_root: Path, tasks_root: Path, agent: str, args) -> list[tuple[Path, object]]:
    tasks = {t.task_id: t for t in list_tasks(tasks_root)}
    agent_root = runs_root / agent
    if not agent_root.exists():
        raise SystemExit(f"No runs folder at {agent_root}")
    found: list[tuple[Path, object]] = []
    for task_dir in sorted(agent_root.iterdir()):
        if args.task_id and task_dir.name != args.task_id:
            continue
        task = tasks.get(task_dir.name)
        if task is None:
            continue
        for condition in ALL_CONDITIONS:
            if args.condition and condition != args.condition:
                continue
            condition_dir = task_dir / condition
            if not condition_dir.exists():
                continue
            for run_dir in sorted(condition_dir.iterdir()):
                if not run_dir.is_dir() or not _judgeable(run_dir, task.repo_before):
                    continue
                found.append((run_dir, task))
    return found


def _condition_of(run_dir: Path) -> str:
    return run_dir.parent.name


def command_judge(args, runs_root: Path, tasks_root: Path) -> int:
    command = [args.exe or "claude", "-p"]
    if args.model:
        command += ["--model", args.model]

    runs = collect_runs(runs_root, tasks_root, args.agent, args)
    pending = [
        (run_dir, task)
        for run_dir, task in runs
        if args.force
        or (read_verdict(run_dir) or {}).get("judge_version") != JUDGE_VERSION
    ]
    cached = len(runs) - len(pending)
    if args.limit:
        pending = pending[: args.limit]

    print(f"Judge command : {command[0]} -p")
    print(f"Judgeable runs: {len(runs)}")
    print(f"To judge      : {len(pending)}  (cached: {cached})")
    if args.dry_run:
        for run_dir, _ in pending[:20]:
            print("  would judge", "/".join(run_dir.parts[-3:]))
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
        return 0
    if not pending:
        print("\nNothing to judge. Report with --mode report.")
        return 0
    print()

    items = list(enumerate(pending, start=1))

    def execute(item):
        index, (run_dir, task) = item
        label = "/".join(run_dir.parts[-3:])
        try:
            verdict = judge_run(
                run_dir, task, command, timeout=args.timeout,
                force=args.force, samples=args.samples,
                reuse=not args.no_reuse, model=args.model or "",
            )
        except Exception as error:  # a judge failure must not abandon the batch
            return False, f"[{index}/{len(pending)}] FAILED {label}: {type(error).__name__}: {error}"
        mark = "fix " if verdict["solved"] else ("cheat" if verdict.get("cheats") else "no  ")
        return True, _safe(f"[{index}/{len(pending)}] {mark} {label}  {verdict.get('reason', '')[:90]}")

    ok_count = fail_count = 0
    if args.parallel > 1:
        from concurrent.futures import ThreadPoolExecutor

        print(f"Judging {args.parallel} at a time\n")
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            for ok, line in pool.map(execute, items):
                ok_count += ok
                fail_count += not ok
                print(line, flush=True)
    else:
        for item in items:
            ok, line = execute(item)
            ok_count += ok
            fail_count += not ok
            print(line, flush=True)

    print(f"\nDone. {ok_count} judged, {fail_count} failed.")
    print(f"Report with:  python scripts/judge_runs.py --agent {args.agent} --mode report")
    return 0


def _rows(runs_root: Path, tasks_root: Path, args) -> list[dict]:
    rows = []
    for run_dir, task in collect_runs(runs_root, tasks_root, args.agent, args):
        verdict = read_verdict(run_dir)
        if verdict is None:
            continue
        rows.append(
            {
                "task_id": task.task_id,
                "condition": _condition_of(run_dir),
                "run": run_dir.name,
                "error_type": task.error_type,
                "run_dir": run_dir,
                **{k: v for k, v in verdict.items() if k not in ("task_id", "run")},
            }
        )
    return rows


def command_report(args, runs_root: Path, tasks_root: Path) -> int:
    rows = _rows(runs_root, tasks_root, args)
    if not rows:
        print("No judgements yet. Run without --mode report first.")
        return 1

    by_condition: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_condition[row["condition"]].append(row)

    print(f"{'condition':<14}{'judged':>8}{'fixed':>8}{'rate':>8}{'cheats':>8}{'low conf':>10}{'unstable':>10}")
    for condition, group in sorted(by_condition.items()):
        solved = sum(1 for r in group if r["solved"])
        print(
            f"{condition:<14}{len(group):>8}{solved:>8}{solved / len(group):>8.2f}"
            f"{sum(1 for r in group if r.get('cheats')):>8}"
            f"{sum(1 for r in group if r.get('confidence') == 'low'):>10}"
            f"{sum(1 for r in group if r.get('unstable')):>10}"
        )

    print()
    print("This table covers only runs that produced a patch, and only the judge's view")
    print("of them. For the rate over every run, and for whichever oracle actually")
    print("decided each one, use:")
    print(f"  python scripts/score_runs.py --agent {args.agent} --mode report")

    by_task: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        # Canonical arm, so the renamed `with_memory` runs pair against `no_memory`
        # instead of forming an arm nothing compares to.
        by_task[(row["task_id"], canonical_condition(row["condition"]))].append(row)

    # Only tasks finished under both conditions. A half-finished task would otherwise
    # contribute to one arm and nothing to the other, inventing an effect out of
    # scheduling order.
    control, memory_arm = PAIR
    tasks_seen = {task for task, _ in by_task}
    paired = sorted(
        t for t in tasks_seen
        if (t, control) in by_task and (t, memory_arm) in by_task
    )
    print(f"\nPass@{args.k} over {len(paired)} task(s) finished in both conditions")
    scores: dict[str, list[float]] = defaultdict(list)
    for task in paired:
        for condition in ALL_CONDITIONS:
            group = by_task.get((task, condition), [])
            if len(group) >= args.k:
                correct = sum(1 for r in group if r["solved"])
                scores[condition].append(pass_at_k(len(group), correct, args.k))
    # Ordered by K, so the arms read as the dose-response sequence they are.
    for condition in sorted(scores, key=lambda c: (condition_k(c), c)):
        print(f"  {condition:<14}{statistics.mean(scores[condition]):.3f}")
    # The named RQ1 contrast, not "whichever two arms turned up". With four arms the
    # old `len(scores) == 2` rule reported nothing, and with two of the wrong ones it
    # would have called the K=1-vs-K=5 gap the memory effect.
    if control in scores and memory_arm in scores:
        delta = statistics.mean(scores[memory_arm]) - statistics.mean(scores[control])
        print(f"\n  memory effect ({control} -> {memory_arm}): {delta:+.3f}")

    print("\nPer task (fixed / judged)")
    print(f"{'task':<26}{control:>14}{memory_arm:>16}")
    for task in sorted(tasks_seen):
        cells = []
        for condition in PAIR:
            group = by_task.get((task, condition), [])
            cells.append(
                f"{sum(1 for r in group if r['solved'])}/{len(group)}" if group else "--"
            )
        print(f"{task:<26}{cells[0]:>14}{cells[1]:>16}")

    mechanisms = defaultdict(int)
    for row in rows:
        mechanisms[row.get("mechanism", "none")] += 1
    print("\nRepair mechanism vs the maintainer's patch: " + ", ".join(
        f"{name} {count}" for name, count in sorted(mechanisms.items(), key=lambda kv: -kv[1])
    ))
    print("'different' runs are exactly what normalized_match scores as failures.")
    return 0


def command_calibration(args, runs_root: Path, tasks_root: Path) -> int:
    """Write a sample of judgements out for a human to check.

    An LLM judge is only usable as a headline metric if its agreement with a human on
    a sample is reported alongside it. The sample is drawn deterministically from a
    hash of the run key, so the same command always produces the same sample and the
    selection cannot be tuned after seeing the results.
    """
    rows = _rows(runs_root, tasks_root, args)
    if not rows:
        print("No judgements yet.")
        return 1
    rows.sort(key=lambda r: hashlib.sha1(
        f"{r['task_id']}/{r['condition']}/{r['run']}".encode("utf-8")
    ).hexdigest())
    sample = rows[: args.sample]
    sample.sort(key=lambda r: (r["task_id"], r["condition"], r["run"]))

    out = [
        "# Judge calibration sample",
        "",
        f"Agent: `{args.agent}`  |  judge version {JUDGE_VERSION}  |  {len(sample)} of {len(rows)} judged runs",
        "",
        "For each run below, decide yourself whether the patch repairs the failure,",
        "then write `human: fix` or `human: no` on the line provided. Agreement between",
        "the two columns is the number to report next to any judge-based result.",
        "",
    ]
    for row in sample:
        task = next(t for t in list_tasks(tasks_root) if t.task_id == row["task_id"])
        diff = workspace_diff(task.repo_before, row["run_dir"] / "workspace", budget=6000)
        out += [
            f"## {row['task_id']} / {row['condition']} / {row['run']}",
            f"Failure type: {', '.join(row['error_type']) or 'unknown'}",
            "",
            f"judge: {'fix' if row['solved'] else 'no'} ({row.get('confidence')}) - {row.get('reason', '')}",
            "",
            "human: ",
            "",
            "```diff",
            diff.strip(),
            "```",
            "",
        ]
    path = runs_root / f"judge_calibration_{args.agent}.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {len(sample)} runs to {path}")
    print("Fill in each 'human:' line, then report agreement in the paper.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Judge whether agent runs actually repair the CI failure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--mode", choices=("judge", "report", "calibration"), default="judge")
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--condition", choices=ALL_CONDITIONS, default=None)
    parser.add_argument("--exe", default=None, help="Full path to the judging CLI binary")
    parser.add_argument("--model", default=None, help="Model for the judge, e.g. sonnet")
    parser.add_argument(
        "--no-reuse",
        action="store_true",
        help=(
            "Judge every run separately even when two produced a byte-identical patch. "
            "Reuse is on by default: an identical judging prompt is literally the same "
            "question, so re-asking it changes no verdict and only spends tokens. Turn "
            "it off to measure the judge's own variance across repeats."
        ),
    )
    parser.add_argument("--limit", type=int, default=0, help="Judge at most N runs, 0 = all")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--force", action="store_true", help="Re-judge runs that already have a verdict")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--k", type=int, default=1, help="k for Pass@K in --mode report")
    parser.add_argument("--sample", type=int, default=20, help="Runs to emit in --mode calibration")
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Independent judge samples per run; the majority wins and the "
             "disagreement is recorded as `agreement`. 1 reproduces the old "
             "single-shot behaviour and should not be used for reported numbers.",
    )
    args = parser.parse_args()

    runs_root = Path(args.runs_root).resolve()
    tasks_root = Path(args.tasks_root).resolve()
    if args.mode == "report":
        return command_report(args, runs_root, tasks_root)
    if args.mode == "calibration":
        return command_calibration(args, runs_root, tasks_root)
    return command_judge(args, runs_root, tasks_root)


if __name__ == "__main__":
    raise SystemExit(main())
