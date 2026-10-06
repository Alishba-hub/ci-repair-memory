"""Prove each instance is a real repair task before spending runs on it.

    python scripts/validate_instances.py --mode forks
    python scripts/validate_instances.py --mode validate --parallel 4
    python scripts/validate_instances.py --mode report

An instance is usable only if two things hold under the standardized workflow:

    the failing commit, untouched, makes CI go red        (there is something to repair)
    the failing commit plus the maintainer's patch goes green  (it is repairable, and the
                                                               harness can observe that)

CI-Repair-Bench's paper states this criterion in Section 3.1 -- "we re-execute the
developer-authored fix under the standardized workflow and retain only instances for
which the CI outcome matches" -- and their released harness never runs it. Skipping it
is not a small omission. An instance that is green before any repair hands both arms a
free success; one whose gold patch cannot turn it green makes every candidate look
wrong and quietly caps the achievable rate below 100%. Either way the memory effect is
measured against a moving floor.

Cost: two Actions runs per instance, once, cached in the task folder. Free on public
repositories, and no model tokens. Compare that with re-judging every run.

Set GITHUB_TOKEN, GITHUB_USERNAME and BENCHMARK_OWNER first. The token needs `repo`
scope and `workflow` scope, the latter because the harness writes .github/workflows.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ci_memory_agents.ci_outcome import CIOutcome, read_outcome, write_outcome
from ci_memory_agents.loader import list_tasks
from ci_memory_agents.oracle import baseline_path, gold_path
from ci_memory_agents.oracle import classify_instance as _classify
from ci_memory_agents.oracle_github import GitHubConfig, OracleError, ensure_fork, run_ci


def _metadata(task) -> dict:
    return json.loads((task.root / "metadata.json").read_text(encoding="utf-8"))


def command_forks(args, tasks) -> int:
    """Fork every repository the task set touches, once."""
    config = GitHubConfig.from_env(Path(args.repos_dir), owner=args.owner)
    wanted: dict[str, str] = {}
    for task in tasks:
        metadata = _metadata(task)
        wanted.setdefault(metadata["repo_name"], metadata["repo_owner"])

    print(f"{len(wanted)} repositories to fork under {config.owner}\n")
    failures = 0
    for repo, upstream in sorted(wanted.items()):
        try:
            full = ensure_fork(config, upstream, repo)
            print(f"  ok   {full}")
        except OracleError as error:
            failures += 1
            print(f"  FAIL {repo}: {error}")
    print(f"\n{len(wanted) - failures} ready, {failures} failed.")
    return 1 if failures else 0


def _validate_one(task, config, args) -> tuple[str, CIOutcome, CIOutcome]:
    """Run the instance twice: untouched, then with the maintainer's patch."""
    metadata = _metadata(task)

    baseline = None if args.force else read_outcome(baseline_path(task.root))
    if baseline is None or not baseline.decided:
        baseline = run_ci(
            config, task, None, "baseline",
            collapse_matrices=args.collapse_matrices,
            drop_non_validation=args.drop_non_validation,
        )
        baseline.source = "github"
        write_outcome(baseline_path(task.root), baseline)

    gold = None if args.force else read_outcome(gold_path(task.root))
    if gold is None or not gold.decided:
        diff = (task.root / "gold_patch.diff").read_text(encoding="utf-8")
        gold = run_ci(
            config, task, diff, "gold",
            collapse_matrices=args.collapse_matrices,
            drop_non_validation=args.drop_non_validation,
        )
        gold.source = "github"
        write_outcome(gold_path(task.root), gold)

    return task.task_id, baseline, gold


def command_validate(args, tasks) -> int:
    config = GitHubConfig.from_env(Path(args.repos_dir), owner=args.owner)
    config.poll_interval = args.poll_interval
    config.poll_timeout = args.poll_timeout

    print(f"Validating {len(tasks)} instances against {config.owner}'s forks")
    print(f"Two CI runs each; {args.parallel} in flight at a time.\n")

    def execute(task):
        try:
            return _validate_one(task, config, args)
        except OracleError as error:
            return task.task_id, CIOutcome("inconclusive", detail=str(error)[:300]), CIOutcome(
                "inconclusive", detail="not attempted"
            )

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for task_id, baseline, gold in pool.map(execute, tasks):
            verdict = _classify(baseline, gold)
            import json as _json
            meta = Path(args.tasks_root) / task_id / "metadata.json"
            dropped = 0
            if meta.exists():
                try:
                    dropped = len(_json.loads(meta.read_text(encoding="utf-8")).get("deselect_tests") or [])
                except (ValueError, OSError):
                    dropped = 0
            note = f"  [{dropped} test(s) deselected]" if dropped else ""
            print(f"  {verdict:<12} {task_id:<28} baseline={baseline.conclusion} gold={gold.conclusion}{note}")
            if verdict != "usable":
                detail = baseline.detail or gold.detail
                if detail:
                    print(f"               {detail[:120]}")

    print("\nDone. Summarise with --mode report.")
    return 0


def command_report(args, tasks) -> int:
    rows = []
    for task in tasks:
        baseline = read_outcome(baseline_path(task.root))
        gold = read_outcome(gold_path(task.root))
        rows.append((task.task_id, _classify(baseline, gold), baseline, gold, task))

    order = {"usable": 0, "already-green": 1, "gold-red": 2, "no-baseline": 3,
             "gold-unknown": 4, "unvalidated": 5}
    rows.sort(key=lambda r: (order.get(r[1], 9), r[0]))

    print(f"{'task':<30}{'status':<15}{'baseline':<14}{'gold':<12}{'error type'}")
    for task_id, status, baseline, gold, task in rows:
        print(
            f"{task_id:<30}{status:<15}"
            f"{(baseline.conclusion if baseline else '-'):<14}"
            f"{(gold.conclusion if gold else '-'):<12}"
            f"{', '.join(task.error_type) or 'unknown'}"
        )

    counts: dict[str, int] = {}
    for _, status, _, _, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    usable = counts.get("usable", 0)
    print(f"\n{usable}/{len(rows)} instances are usable repair tasks.")
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        if status != "usable":
            print(f"  {count:>3} {status}")

    if usable < len(rows):
        print(
            "\nExcluded instances must be dropped from BOTH arms before any rate is "
            "computed, and the exclusion count reported in the paper. Write the usable "
            "set out with --mode report --write-manifest."
        )
    if args.write_manifest:
        manifest = REPO_ROOT / "tasks" / "usable_instances.json"
        payload = {
            "usable": [task_id for task_id, status, *_ in rows if status == "usable"],
            "excluded": {
                task_id: status for task_id, status, *_ in rows if status != "usable"
            },
        }
        manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {manifest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate that each instance actually fails, and that its gold patch fixes it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=("forks", "validate", "report"), default="report")
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--source", default="ci-repair-bench")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--repos-dir", default=str(REPO_ROOT / "runs" / "_repos"))
    parser.add_argument("--owner", default=None, help="GitHub account holding the forks")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--poll-timeout", type=int, default=3600)
    parser.add_argument("--force", action="store_true", help="Re-run instances already validated")
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument(
        "--collapse-matrices",
        action="store_true",
        help="Reduce each matrix dimension to one value. Cheaper, but departs from the "
             "benchmark harness; say so in the paper if used.",
    )
    parser.add_argument(
        "--drop-non-validation",
        action="store_true",
        help="Remove publish/upload/notify steps, as the CI-Repair-Bench paper describes "
             "and its code does not.",
    )
    args = parser.parse_args()

    tasks = list_tasks(Path(args.tasks_root), source=args.source or None)
    if args.task_id:
        tasks = [task for task in tasks if task.task_id == args.task_id]
        if not tasks:
            raise SystemExit(f"no task named {args.task_id}")
    if not tasks:
        raise SystemExit(f"no tasks under {args.tasks_root}")

    if args.mode == "forks":
        return command_forks(args, tasks)
    if args.mode == "validate":
        return command_validate(args, tasks)
    return command_report(args, tasks)


if __name__ == "__main__":
    raise SystemExit(main())
