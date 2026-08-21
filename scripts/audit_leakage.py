from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents.loader import list_tasks
from ci_memory_agents.prompt_builder import CONDITIONS, build_prompt

WHITESPACE = re.compile(r"\s+")

BOILERPLATE = re.compile(
    r"^(except\s+\w*(Exception|Error)?\s*:|try\s*:|else\s*:|pass|return|raise|import\s|from\s+\w+\s+import\s)",
)


def added_lines(diff: str, min_length: int = 15) -> list[str]:
    """Substantive lines the gold patch introduces."""
    lines = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:].strip()
        if len(text) >= min_length:
            lines.append(text)
    return lines


def normalize(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def audit_task(task) -> dict:
    """Measure how much of the gold patch is visible in each condition's prompt.

    Whitespace is normalised before comparison. Without that, a patch that only
    strips a trailing space looks like a leak, because the added line matches a line
    the agent can already see. Language boilerplate is excluded: `except Exception:`
    appearing in a prior patch is a coding convention, not the answer.
    """
    gold = task.root / "gold_patch.diff"
    if not gold.exists():
        return {}
    baseline = normalize(
        "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(task.repo_before.rglob("*"))
            if path.is_file()
        )
    )
    candidates = [line for line in added_lines(gold.read_text(encoding="utf-8"))
                  if not BOILERPLATE.match(line)]
    if not candidates:
        return {}

    report = {"task_id": task.task_id, "gold_lines": len(candidates), "conditions": {}}
    for condition in CONDITIONS:
        prompt = normalize(build_prompt(task, condition))
        leaked = [
            line for line in candidates
            if normalize(line) in prompt and normalize(line) not in baseline
        ]
        report["conditions"][condition] = {
            "leaked": leaked,
            "ratio": len(leaked) / len(candidates),
        }
    return report


def audit_memory_ordering(task) -> list[str]:
    problems = []
    for item in task.memory:
        if task.commit_date and item.commit_date >= task.commit_date:
            problems.append(
                f"{task.task_id}: memory {item.instance_id} dated {item.commit_date} "
                f"is not earlier than target {task.commit_date}"
            )
    return problems


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Audit prompts for gold-patch leakage")
    parser.add_argument("--tasks-root", default=str(repo_root / "tasks"))
    parser.add_argument("--source", default="ci-repair-bench")
    parser.add_argument(
        "--max-overlap",
        type=float,
        default=0.25,
        help="Per-task fraction of gold lines allowed to appear in a prompt",
    )
    args = parser.parse_args()

    tasks = list_tasks(Path(args.tasks_root), source=args.source)
    ordering: list[str] = []
    reports = []
    for task in tasks:
        ordering.extend(audit_memory_ordering(task))
        report = audit_task(task)
        if report:
            reports.append(report)

    print(f"Audited {len(tasks)} tasks x {len(CONDITIONS)} conditions\n")
    print(f"{'task':<32}{'gold':>6}{'no_mem':>9}{'with_mem':>10}")
    failures = []
    for report in sorted(reports, key=lambda r: -r["conditions"]["with_memory"]["ratio"]):
        no_memory = report["conditions"]["no_memory"]["ratio"]
        with_memory = report["conditions"]["with_memory"]["ratio"]
        flag = " <-- over threshold" if with_memory > args.max_overlap else ""
        if with_memory > args.max_overlap:
            failures.append(report)
        if with_memory or no_memory:
            print(f"{report['task_id']:<32}{report['gold_lines']:>6}{no_memory:>9.0%}{with_memory:>10.0%}{flag}")

    contaminated = [r for r in reports if r["conditions"]["with_memory"]["ratio"] > 0]
    print(
        f"\n{len(contaminated)}/{len(reports)} tasks show any residual overlap; "
        f"{len(failures)} exceed the {args.max_overlap:.0%} threshold"
    )
    if ordering:
        print(f"\n{len(ordering)} ORDERING VIOLATIONS:")
        for problem in ordering:
            print(f"  - {problem}")
    return 1 if failures or ordering else 0


if __name__ == "__main__":
    raise SystemExit(main())
