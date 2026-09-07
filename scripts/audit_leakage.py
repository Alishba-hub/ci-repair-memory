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


def gold_files(task) -> list[str]:
    """Paths the reference patch touches, as they appear in the diff headers."""
    names = set()
    for line in (task.root / "gold_patch.diff").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("+++ ", "--- ")):
            path = line[4:].strip()
            if path in ("/dev/null", ""):
                continue
            names.add(path[2:] if path[:2] in ("a/", "b/") else path)
    return sorted(names)


def audit_localization(task, prompt: str, baseline_files: set[str]) -> list[str]:
    """Files the fix touches that the memory block names but the repository does not.

    Content overlap is not the only way a memory block can carry the answer. A prior
    patch that happens to touch the same file as the target's fix tells the agent where
    to look, and only one arm gets that hint. Under the focused scope the leak is inert,
    because `repo_before` holds exactly the fix's files in every arm and there is
    nothing left to localise. Under the full-repository scope it is a real advantage
    that overlap measured in added lines will never see, so it is measured separately
    rather than folded into the same ratio.
    """
    return [
        name
        for name in gold_files(task)
        if name not in baseline_files and name in prompt
    ]


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

    baseline_files = {
        str(path.relative_to(task.repo_before)).replace("\\", "/")
        for path in task.repo_before.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }

    report = {
        "task_id": task.task_id,
        "gold_lines": len(candidates),
        "conditions": {},
    }
    for condition in CONDITIONS:
        raw = build_prompt(task, condition)
        prompt = normalize(raw)
        leaked = [
            line for line in candidates
            if normalize(line) in prompt and normalize(line) not in baseline
        ]
        report["conditions"][condition] = {
            "leaked": leaked,
            "ratio": len(leaked) / len(candidates),
            "localization": audit_localization(task, raw, baseline_files),
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

    audited = sorted({c for r in reports for c in r["conditions"]}, key=CONDITIONS.index)
    print(f"Audited {len(tasks)} tasks x {len(audited)} conditions: {', '.join(audited)}\n")

    print("Content overlap -- share of the gold patch's added lines visible in the prompt")
    print(f"{'task':<32}{'gold':>6}{'no_mem':>9}{'with_mem':>10}")
    failures = []
    for report in sorted(reports, key=lambda r: -r["conditions"]["with_memory"]["ratio"]):
        ratios = {c: report["conditions"][c]["ratio"] for c in report["conditions"]}
        over = [c for c, ratio in ratios.items() if ratio > args.max_overlap]
        if over:
            failures.append(report)
        if any(ratios.values()):
            cells = "".join(
                f"{ratios.get(c, 0):>{w}.0%}"
                for c, w in (("no_memory", 9), ("with_memory", 10))
            )
            flag = f" <-- over threshold in {', '.join(over)}" if over else ""
            print(f"{report['task_id']:<32}{report['gold_lines']:>6}{cells}{flag}")

    contaminated = [r for r in reports if r["conditions"]["with_memory"]["ratio"] > 0]
    print(
        f"\n{len(contaminated)}/{len(reports)} tasks show any residual overlap; "
        f"{len(failures)} exceed the {args.max_overlap:.0%} threshold"
    )

    # Localisation leaks separately. This is not the same quantity as content overlap and
    # must not be averaged into it: a memory item can name the file the fix belongs in
    # while sharing none of its lines.
    print("\nLocalization signal -- fix's files named in the prompt but absent from repo_before")
    localized = [
        (r["task_id"], condition, names)
        for r in reports
        for condition, data in r["conditions"].items()
        if (names := data["localization"])
    ]
    if not localized:
        print("  none. Under the focused scope repo_before already holds the fix's files,")
        print("  so there is no localisation left to leak. Re-run this after")
        print("  scripts/materialize_repos.py: under the full scope it can become real.")
    else:
        for task_id, condition, names in localized:
            print(f"  {task_id:<30}{condition:<16}{', '.join(names)}")
        print(f"  {len(localized)} task-condition pairs leak a path the agent would otherwise hunt for.")

    if ordering:
        print(f"\n{len(ordering)} ORDERING VIOLATIONS:")
        for problem in ordering:
            print(f"  - {problem}")
    return 1 if failures or ordering else 0


if __name__ == "__main__":
    raise SystemExit(main())
