from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents.importer import ImportReport, build_task
from ci_memory_agents.memory import select_tasks

PARQUET_URL = (
    "https://huggingface.co/datasets/ci-benchmark-user/ci-repair-bench/"
    "resolve/refs%2Fconvert%2Fparquet/default/train/0000.parquet"
)

COLUMNS = [
    "id",
    "language",
    "repo_owner",
    "repo_name",
    "sha_fail",
    "sha_success",
    "workflow",
    "workflow_filename",
    "workflow_path",
    "logs",
    "diff",
    "changed_files",
    "error_type",
    "commit_date",
]


def load_rows(parquet_path: Path) -> list[dict]:
    import pyarrow.parquet as pq

    if not parquet_path.exists():
        print(f"Downloading CI-Repair-Bench parquet to {parquet_path} (~240 MB)...")
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(PARQUET_URL, parquet_path)

    table = pq.read_table(parquet_path, columns=COLUMNS)
    columns = table.to_pydict()
    return [dict(zip(COLUMNS, values)) for values in zip(*(columns[name] for name in COLUMNS))]


def _balance_projects(selections: list, max_per_project: int) -> list:
    """Round-robin across projects so one large repo cannot dominate the sample.

    agno alone contributes 84 of the 567 instances; taking candidates in project
    order would make the results a study of agno rather than of CI repair.
    """
    from collections import defaultdict

    buckets: dict[str, list] = defaultdict(list)
    for selection in selections:
        name = selection.target["repo_name"]
        if len(buckets[name]) < max_per_project:
            buckets[name].append(selection)

    ordered: list = []
    position = 0
    while any(len(items) > position for items in buckets.values()):
        for name in sorted(buckets):
            if len(buckets[name]) > position:
                ordered.append(buckets[name][position])
        position += 1
    return ordered


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import CI-Repair-Bench instances as local tasks")
    parser.add_argument("--parquet", default=str(repo_root / "data" / "ci-repair-bench.parquet"))
    parser.add_argument("--tasks-root", default=str(repo_root / "tasks"))
    parser.add_argument("--limit", type=int, default=20, help="Number of tasks to materialise")
    parser.add_argument("--min-prior", type=int, default=3, help="Required earlier failures per project")
    parser.add_argument("--memory-size", type=int, default=3, help="Prior failures kept as memory")
    parser.add_argument("--max-files", type=int, default=3, help="Maximum files the gold patch may touch")
    parser.add_argument("--max-per-project", type=int, default=3, help="Cap tasks taken per project")
    parser.add_argument("--log-budget", type=int, default=3000, help="Characters per compressed log")
    parser.add_argument(
        "--all-error-types",
        action="store_true",
        help="Include formatting/linting-only targets (excluded by default)",
    )
    args = parser.parse_args()

    rows = load_rows(Path(args.parquet))
    print(f"Loaded {len(rows)} CI-Repair-Bench instances")

    selections = select_tasks(
        rows,
        min_prior=args.min_prior,
        memory_size=args.memory_size,
        max_files=args.max_files,
        semantic_only=not args.all_error_types,
    )
    print(
        f"{len(selections)} instances have >={args.min_prior} strictly earlier same-project "
        f"failures, touch <={args.max_files} files"
        + ("" if args.all_error_types else ", and fail for a non-formatting reason")
    )

    selections = _balance_projects(selections, args.max_per_project)
    print(f"After balancing to <={args.max_per_project} per project: {len(selections)} candidates")

    tasks_root = Path(args.tasks_root)
    tasks_root.mkdir(parents=True, exist_ok=True)
    report = ImportReport()

    for selection in selections:
        if len(report.imported) >= args.limit:
            break
        target = selection.target
        task_id = f"crb_{target['repo_name']}_{target['id']}"
        task_dir = tasks_root / task_id
        error = build_task(target, selection.memory, task_dir, log_budget=args.log_budget)
        if error:
            report.skip(task_id, error)
            print(f"  skip {task_id}: {error}")
        else:
            report.imported.append(task_id)
            print(f"  ok   {task_id} ({', '.join(target['error_type'] or []) or 'unknown'})")

    print(f"\nImported {len(report.imported)} tasks, skipped {len(report.skipped)}")
    return 0 if report.imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
