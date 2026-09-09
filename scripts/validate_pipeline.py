from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents.evaluator import evaluate_submission, pass_at_k
from ci_memory_agents.loader import list_tasks

BASELINES = ("oracle", "noop", "half")


def build_submission(task, baseline: str, destination: Path) -> None:
    """Construct a synthetic agent submission with a known correctness level."""
    source = task.repo_after if baseline == "oracle" else task.repo_before
    shutil.copytree(source, destination)
    if baseline != "half":
        return

    def _text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, OSError):
            return None

    # A file the gold patch *creates* has no counterpart in repo_before, and reading one
    # unconditionally raised FileNotFoundError and took the whole calibration down --
    # crb_taipy_437's fix adds taipy.sqlite3.db. A created file is a changed file: it is
    # absent on one side and present on the other, which is exactly the comparison being
    # made, so it belongs in `gold_changed` rather than being an error.
    gold_changed = [
        path
        for path in sorted(task.repo_after.rglob("*"))
        if path.is_file()
        and _text(task.repo_before / path.relative_to(task.repo_after)) != _text(path)
    ]
    for path in gold_changed[: max(1, len(gold_changed) // 2)]:
        relative = path.relative_to(task.repo_after)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Calibrate the scorer with synthetic baselines")
    parser.add_argument("--tasks-root", default=str(repo_root / "tasks"))
    parser.add_argument("--source", default="ci-repair-bench")
    args = parser.parse_args()

    tasks = list_tasks(Path(args.tasks_root), source=args.source)
    if not tasks:
        print("No tasks found. Run scripts/import_ci_repair_bench.py first.")
        return 1

    print(f"Calibrating on {len(tasks)} tasks\n")
    print(f"{'baseline':<10}{'exact':>8}{'norm':>8}{'IoU':>8}{'recall':>9}{'linedev':>9}")

    failures = []
    for baseline in BASELINES:
        results = []
        for task in tasks:
            with tempfile.TemporaryDirectory() as workdir:
                submission = Path(workdir) / "submission"
                build_submission(task, baseline, submission)
                results.append(
                    evaluate_submission(
                        submission, task.repo_before, task.repo_after, task.task_id, baseline
                    )
                )
        exact = sum(1 for r in results if r.exact_match) / len(results)
        normalized = sum(1 for r in results if r.normalized_match) / len(results)
        iou = statistics.mean(r.file_iou for r in results)
        recall = statistics.mean(r.file_recall for r in results)
        deviation = statistics.median(r.line_deviation_ratio for r in results)
        print(f"{baseline:<10}{exact:>8.2f}{normalized:>8.2f}{iou:>8.2f}{recall:>9.2f}{deviation:>9.2f}")

        if baseline == "oracle" and exact < 1.0:
            failures.append(f"oracle scored {exact:.2f} exact match, expected 1.00")
        if baseline == "noop" and exact > 0.0:
            failures.append(f"noop scored {exact:.2f} exact match, expected 0.00")

    print("\nPass@K estimator check (n=10 runs)")
    for correct in (0, 1, 5, 10):
        row = "  ".join(f"k={k}: {pass_at_k(10, correct, k):.2f}" for k in (1, 3, 5))
        print(f"  {correct:>2}/10 runs correct -> {row}")

    if failures:
        print("\nCALIBRATION FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nScorer calibrated: oracle=1.00 exact, noop=0.00 exact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
