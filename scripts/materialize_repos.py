"""Give each task the real repository at the failing commit, not just the gold files.

    python scripts/materialize_repos.py --plan
    python scripts/materialize_repos.py --apply --limit 5
    python scripts/materialize_repos.py --apply --prune-vendor

Why this exists
---------------
`importer.build_task` fetches, from raw.githubusercontent, exactly the files the gold
patch touches, and calls that `repo_before`. Two things follow, and both are visible in
the pilot data.

The measurement problem: the agent is handed the set of files the maintainers changed.
Fault localization -- which CI-Repair-Bench treats as a task in its own right and
reports Top-1/Top-3/Top-5 and MAP for, and at which the strongest model in their paper
scores 45% Top-1 -- is not merely made easier, it is solved in advance and for free. A
result measured this way cannot be compared with theirs, and a reviewer who notices will
not be persuaded that the memory effect survives giving the agent a real repository,
because memory's most plausible mechanism is helping it find the right file.

The bigger problem, which the pilot data makes concrete: the agent usually cannot act at
all. Across the 480 completed claude-code runs, 338 (70%) changed no file. On the 21
tasks whose `repo_before` holds one to three files, the empty-patch rate is 80-90%; on
the 3 tasks where the fix happens to be self-contained, it is 0%. The agents say why in
their replies -- "I couldn't read httpx/_config.py (not in the provided file set)". They
were asked to repair a build while holding a few files out of a repository, and declined
to invent a change. That is the correct behaviour and it is the harness's fault.

Those 3 tasks are exactly the 3 the pilot's 69.6%/78% was computed over.

What this does
--------------
Replaces each `tasks/<id>/repo_before` with a real checkout at `sha_fail`, fetched one
commit deep. The gold-file tree is kept alongside as `repo_before_focused`, so the pilot
remains reproducible and the two scopes can be compared -- which is itself worth a table,
since the difference between them is the size of the localization giveaway.
`repo_after` is not materialised: `gold_patch.diff` is the reference, and duplicating a
whole tree per task to hold a handful of changed lines is not worth the disk.

Disk is the real cost. `--plan` reports it before anything is written, and
`--max-checkout-mb` skips repositories above a threshold so one CPython does not consume
the budget for twenty smaller instances.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ci_memory_agents.loader import list_tasks

# Directories that are checked in but are not the project: vendored dependencies and
# test fixtures that inflate a checkout without helping an agent locate a CI failure.
VENDOR = ("vendor", "third_party", "3rdparty", "node_modules", "external", ".tox")


def _run(args: list[str], cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )


def checkout(owners: list[str], repo: str, sha: str, destination: Path) -> str:
    """The working tree at `sha`, fetched shallowly straight from GitHub.

    `git fetch --depth 1 origin <sha>` is the only reliable way to obtain one arbitrary
    historical commit: you cannot clone at a sha, and a full clone of something the size
    of CPython to reach one tree is not a trade worth making. The cost is one tree per
    task rather than one per repository, which is the right way round -- disk is cheap
    and a partial-clone promisor shared across tasks is a source of failures that only
    appear halfway through a batch.

    `owners` is tried in order because the benchmark rows name forks under the authors'
    accounts, which may or may not still carry the commit; the upstream project is the
    fallback. Returns the owner that worked.
    """
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=destination)

    last = ""
    for owner in owners:
        _run(["git", "remote", "remove", "origin"], cwd=destination)
        _run(["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"],
             cwd=destination)
        fetch = _run(["git", "fetch", "--depth", "1", "--quiet", "origin", sha], cwd=destination)
        if fetch.returncode != 0:
            last = fetch.stderr.strip()[:200]
            continue
        out = _run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=destination)
        if out.returncode == 0:
            return owner
        last = out.stderr.strip()[:200]
    raise RuntimeError(f"no owner in {owners} yielded {sha[:8]}: {last}")


def prune_vendor(root: Path) -> int:
    removed = 0
    for name in VENDOR:
        for path in root.rglob(name):
            if path.is_dir() and ".git" not in path.parts:
                removed += sum(1 for _ in path.rglob("*"))
                shutil.rmtree(path, ignore_errors=True)
    return removed


def size_mb(root: Path) -> float:
    total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    return round(total / (1024 * 1024), 1)


def command_plan(tasks, args) -> int:
    repos: dict[str, list[str]] = {}
    for task in tasks:
        metadata = json.loads((task.root / "metadata.json").read_text(encoding="utf-8"))
        repos.setdefault(metadata["repo_name"], []).append(task.task_id)

    print(f"{len(tasks)} tasks across {len(repos)} repositories.\n")
    print(f"{'repository':<28}{'instances':>10}{'current files':>15}")
    for repo, ids in sorted(repos.items(), key=lambda kv: -len(kv[1])):
        current = sum(
            sum(1 for p in (REPO_ROOT / "tasks" / task_id / "repo_before").rglob("*") if p.is_file())
            for task_id in ids
        )
        print(f"{repo:<28}{len(ids):>10}{current:>15}")

    print(
        "\nEach task gets its own shallow checkout under tasks/<id>/repo_before, fetched\n"
        "with `git fetch --depth 1 origin <sha_fail>`: one commit, no history.\n"
        "\nRun with --apply to materialise. Start with --limit 3 and check the disk cost\n"
        f"against your budget before doing all {len(tasks)}."
    )
    return 0


def command_apply(tasks, args) -> int:
    done = failed = skipped = 0
    total_mb = 0.0

    for task in tasks:
        if args.limit and done >= args.limit:
            break
        metadata = json.loads((task.root / "metadata.json").read_text(encoding="utf-8"))
        if metadata.get("repo_scope") == "full" and not args.force:
            skipped += 1
            continue

        repo, sha = metadata["repo_name"], metadata["sha_fail"]
        owner = metadata.get("repo_owner") or repo
        destination = task.root / "repo_before"
        backup = task.root / "repo_before_focused"

        # Keep the focused tree: it is what the pilot numbers were produced against, and
        # a comparison between the two scopes is a result in itself.
        if destination.exists() and not backup.exists():
            shutil.move(str(destination), str(backup))

        owners = [owner, repo] if owner != repo else [repo]
        try:
            resolved = checkout(owners, repo, sha, destination)
        except Exception as error:
            print(f"  FAIL {task.task_id}: {error}")
            if backup.exists() and not destination.exists():
                shutil.move(str(backup), str(destination))
            failed += 1
            continue

        if args.prune_vendor:
            prune_vendor(destination)

        megabytes = size_mb(destination)
        if args.max_checkout_mb and megabytes > args.max_checkout_mb:
            print(f"  skip {task.task_id}: checkout is {megabytes} MB, over --max-checkout-mb")
            shutil.rmtree(destination, ignore_errors=True)
            if backup.exists():
                shutil.move(str(backup), str(destination))
            skipped += 1
            continue

        files = sum(1 for p in destination.rglob("*") if p.is_file() and ".git" not in p.parts)
        metadata["repo_scope"] = "full"
        metadata["repo_checkout_owner"] = resolved
        metadata["repo_files"] = files
        metadata["repo_checkout_mb"] = megabytes
        (task.root / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        total_mb += megabytes
        done += 1
        print(f"  ok   {task.task_id:<28}{files:>7} files{megabytes:>9.1f} MB")

    print(f"\n{done} materialised, {skipped} skipped, {failed} failed. {total_mb:.1f} MB written.")
    if done:
        print(
            "\nThe focused trees are kept as tasks/<id>/repo_before_focused. Prompts and\n"
            "the judge read repo_before, so both now see the real repository.\n"
            "Regenerate prompts before running any agent:\n"
            "  python scripts/run_experiment.py --mode prompts --agent claude-code --runs 5"
        )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace gold-file-only task trees with real checkouts at sha_fail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--source", default="ci-repair-bench")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--plan", action="store_true", help="Report the cost and change nothing")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Re-materialise tasks already done")
    parser.add_argument("--prune-vendor", action="store_true")
    parser.add_argument("--max-checkout-mb", type=float, default=0.0)
    args = parser.parse_args()

    tasks = list_tasks(Path(args.tasks_root), source=args.source or None)
    if args.task_id:
        tasks = [t for t in tasks if t.task_id == args.task_id]
    if not tasks:
        raise SystemExit("no tasks selected")

    if args.apply:
        return command_apply(tasks, args)
    return command_plan(tasks, args)


if __name__ == "__main__":
    raise SystemExit(main())
