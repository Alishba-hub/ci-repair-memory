"""Materialise the study's task population from the CI-Repair-Bench parquet.

By default this imports exactly the design in `ci_memory_agents.design`: N_REPOS
repositories, TASKS_PER_REPO tasks each, stratified over the three problem groups, each
task carrying MEMORY_SIZE earlier (failure log, gold patch) pairs so the K=1/3/5 arms
are all fillable. The flags below only widen or narrow that; they do not change what a
task is.

    python scripts/import_ci_repair_bench.py              # the 30-task design
    python scripts/import_ci_repair_bench.py --n-repos 4  # a smaller pilot

Import can fail per instance -- the repository may have been deleted, or the failing
commit garbage-collected -- so a failed task is replaced by the next eligible target
from the same repository. Without substitution one dead URL would leave a repository
with two tasks and quietly break the balance the selection just established.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents import design
from ci_memory_agents.importer import ImportReport, build_task
from ci_memory_agents.memory import select_study_tasks, select_tasks

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


def task_id_of(target: dict) -> str:
    return f"crb_{target['repo_name']}_{target['id']}"


def report_grid(selections: list, heading: str) -> None:
    """The repo x problem-group grid, printed so the population is visible up front."""
    by_repo: dict[str, Counter] = defaultdict(Counter)
    for selection in selections:
        group = design.error_group(selection.target["error_type"]) or "unclassified"
        by_repo[selection.target["repo_name"]][group] += 1

    groups = list(design.GROUP_NAMES)
    width = max((len(r) for r in by_repo), default=10)
    print(f"\n{heading}")
    print(f"  {'repository':<{width}}  " + "  ".join(f"{g:>15}" for g in groups) + "  total")
    for repo in sorted(by_repo):
        counts = by_repo[repo]
        cells = "  ".join(f"{counts.get(g, 0):>15}" for g in groups)
        print(f"  {repo:<{width}}  {cells}  {sum(counts.values()):>5}")
    totals = Counter()
    for counts in by_repo.values():
        totals.update(counts)
    cells = "  ".join(f"{totals.get(g, 0):>15}" for g in groups)
    print(f"  {'TOTAL':<{width}}  {cells}  {sum(totals.values()):>5}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import CI-Repair-Bench instances as local tasks")
    parser.add_argument("--parquet", default=str(repo_root / "data" / "ci-repair-bench.parquet"))
    parser.add_argument("--tasks-root", default=str(repo_root / "tasks"))
    parser.add_argument("--n-repos", type=int, default=design.N_REPOS)
    parser.add_argument("--tasks-per-repo", type=int, default=design.TASKS_PER_REPO)
    parser.add_argument(
        "--min-prior",
        type=int,
        default=design.MIN_PRIOR,
        help="Required earlier failures per project; must be >= max(K) or the K=5 arm cannot be filled",
    )
    parser.add_argument("--memory-size", type=int, default=design.MEMORY_SIZE,
                        help="Prior failures stored per task; the K arms take prefixes of these")
    parser.add_argument("--max-files", type=int, default=design.MAX_FILES)
    parser.add_argument(
        "--repo-slack",
        type=int,
        default=4,
        help=(
            "Extra repositories to select beyond --n-repos, as substitutes for any "
            "whose gold patches will not apply. They are consumed in the selector's "
            "own preference order, so a substitute is never an arbitrary choice."
        ),
    )
    parser.add_argument("--log-budget", type=int, default=3000, help="Characters per compressed log")
    parser.add_argument(
        "--all-error-types",
        action="store_true",
        help="Include formatting/linting-only targets (excluded by default)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the population and stop")
    args = parser.parse_args()

    if args.memory_size < max(design.K_VALUES):
        parser.error(
            f"--memory-size {args.memory_size} is below max(K)={max(design.K_VALUES)}; "
            f"the K={max(design.K_VALUES)} arm would be unfillable"
        )

    rows = load_rows(Path(args.parquet))
    print(f"Loaded {len(rows)} CI-Repair-Bench instances")

    filters = dict(
        min_prior=args.min_prior,
        memory_size=args.memory_size,
        max_files=args.max_files,
        semantic_only=not args.all_error_types,
    )
    pool = select_tasks(rows, **filters)
    print(
        f"{len(pool)} instances have >={args.min_prior} strictly earlier same-project failures, "
        f"touch <={args.max_files} files"
        + ("" if args.all_error_types else ", and fail for a non-formatting reason")
    )

    # The design population is selected first, at exactly --n-repos, and is never
    # reshaped by the slack. Selecting n_repos + slack in one call looked equivalent and
    # is not: the selector optimises group balance across whatever set it is given, so
    # asking for four extra repositories changed which tasks the first ten received.
    # The design would then have depended on the size of its own substitute bench.
    selections = select_study_tasks(
        rows, n_repos=args.n_repos, tasks_per_repo=args.tasks_per_repo, **filters
    )
    report_grid(selections, f"Study population: {len(selections)} tasks")

    primary_repos = list(dict.fromkeys(s.target["repo_name"] for s in selections))

    # Reserve repositories, appended after the design's own. Drawn from a wider call and
    # filtered to repositories the design did not already take, so a substitute is still
    # the next repository the selector would have chosen -- but nothing above it moves.
    reserves: list = []
    if args.repo_slack > 0:
        try:
            wider = select_study_tasks(
                rows,
                n_repos=args.n_repos + args.repo_slack,
                tasks_per_repo=args.tasks_per_repo,
                **filters,
            )
        except ValueError:
            wider = []
        reserves = [s for s in wider if s.target["repo_name"] not in primary_repos]
        reserve_repos = list(dict.fromkeys(s.target["repo_name"] for s in reserves))
        if reserve_repos:
            print(f"  reserve repositories, used only if one above fails: "
                  f"{', '.join(reserve_repos)}")
        else:
            print("  no reserve repositories available: a repository whose patches fail "
                  "to apply will shrink the design.")

    selections = list(selections) + reserves

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    # Import repository by repository, and treat a repository that cannot fill its
    # quota the same way a failed task is treated: replace it.
    #
    # A gold patch that will not apply to the fetched tree is common -- the diff is
    # against the full repository and the focused scope holds only the files it touches
    # -- and the old loop substituted a *task* from the same repository but never the
    # repository itself. When camel's three targets all failed and its spares ran out,
    # the design silently returned nine repositories instead of ten, which is not a
    # smaller version of the study but a different population.
    #
    # Repositories are over-selected by `--repo-slack` and consumed in the selector's
    # preference order until `--n-repos` of them are full, so the substitute is the
    # next repository the selector would itself have chosen.
    chosen_ids = {str(s.target["id"]) for s in selections}
    spare: dict[str, list] = defaultdict(list)
    for selection in sorted(pool, key=lambda s: (s.target["commit_date"], s.target["id"])):
        if str(selection.target["id"]) not in chosen_ids:
            spare[selection.target["repo_name"]].append(selection)

    by_repo: dict[str, list] = defaultdict(list)
    for selection in selections:
        by_repo[selection.target["repo_name"]].append(selection)

    tasks_root = Path(args.tasks_root)
    tasks_root.mkdir(parents=True, exist_ok=True)
    report = ImportReport()
    imported: list = []
    complete: list[str] = []
    abandoned: list[str] = []

    for repo, repo_selections in by_repo.items():
        if len(complete) >= args.n_repos:
            break

        got: list = []
        queue = list(repo_selections)
        while queue and len(got) < args.tasks_per_repo:
            selection = queue.pop(0)
            target = selection.target
            task_id = task_id_of(target)
            error = build_task(
                target, selection.memory, tasks_root / task_id, log_budget=args.log_budget
            )
            if not error:
                got.append(selection)
                print(f"  ok   {task_id} ({', '.join(target['error_type'] or []) or 'unknown'})")
                continue

            report.skip(task_id, error)
            if spare[repo]:
                replacement = spare[repo].pop(0)
                print(f"  skip {task_id}: {error}")
                print(f"       substituting {task_id_of(replacement.target)} from the same repository")
                queue.append(replacement)
            else:
                print(f"  skip {task_id}: {error} (no substitute left in {repo})")

        if len(got) >= args.tasks_per_repo:
            complete.append(repo)
            imported.extend(got)
            report.imported.extend(task_id_of(s.target) for s in got)
        else:
            # Partial repositories are dropped from the design rather than shipped
            # short. Their folders stay on disk -- they cost network to build and may
            # be useful -- but they are not in the manifest, so nothing lays them out.
            abandoned.append(repo)
            print(
                f"  --   {repo} filled only {len(got)}/{args.tasks_per_repo}; "
                f"dropping it and taking the next repository instead"
            )

    if len(complete) < args.n_repos:
        print(
            f"{chr(10)}WARNING: only {len(complete)} of {args.n_repos} repositories could be "
            f"filled to {args.tasks_per_repo} tasks. Raise --repo-slack so the selector "
            f"offers more substitutes, or lower --n-repos."
        )

    # The manifest. `tasks/` accumulates: a task imported under an earlier design stays
    # on disk, and nothing in the folder itself says whether it belongs to the current
    # study. Without this file the only way to tell a design task from a leftover was to
    # re-derive the selection from the parquet, so every consumer either re-read 240 MB
    # or silently ran whatever `tasks/*` happened to contain.
    manifest = {
        "source": "ci-repair-bench",
        "n_repos": args.n_repos,
        "tasks_per_repo": args.tasks_per_repo,
        "memory_size": args.memory_size,
        "min_prior": args.min_prior,
        "max_files": args.max_files,
        "semantic_only": not args.all_error_types,
        "k_values": list(design.K_VALUES),
        "task_ids": sorted(report.imported),
    }
    manifest_path = tasks_root / "study_population.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + chr(10), encoding="utf-8")

    print(f"\nImported {len(report.imported)} tasks, skipped {len(report.skipped)}")
    print(f"Wrote {manifest_path.name} -- the {len(report.imported)} tasks of this design")

    leftovers = sorted(
        d.name for d in tasks_root.glob("crb_*")
        if d.is_dir() and d.name not in set(report.imported)
    )
    if leftovers:
        shown = ", ".join(leftovers[:5]) + (" ..." if len(leftovers) > 5 else "")
        print(f"{len(leftovers)} task folder(s) on disk are NOT in this design: {shown}")
        print("They are kept -- runs collected against them are real data -- but the")
        print("harness selects from the manifest, so they will not be laid out again.")
    if imported:
        report_grid(imported, "Imported population")

    short = [s.target["repo_name"] for s in imported]
    undersized = sorted(
        repo for repo, count in Counter(short).items() if count < args.tasks_per_repo
    )
    if undersized:
        print(
            f"\nWARNING: {', '.join(undersized)} ended up below {args.tasks_per_repo} tasks. "
            "The design's repository balance is not satisfied; re-run to retry the failures, "
            "or lower --tasks-per-repo."
        )
    return 0 if report.imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
