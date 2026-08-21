from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


TRIVIAL_ERROR_TYPES = frozenset(
    {"Code Formatting", "Code Linting", "Documentation or Docstring Error"}
)

SEMANTIC_ERROR_TYPES = frozenset(
    {
        "Test Failure",
        "Assertion Error",
        "Runtime Error",
        "Syntax Error",
        "Type Checking Error",
        "Dependency Issues",
        "Package Installation Error",
        "Configuration Error",
        "Environment Error",
    }
)


class LeakageError(RuntimeError):
    """Raised when a memory item would reveal the fix for the target instance."""


@dataclass(frozen=True)
class Selection:
    target: dict
    memory: list[dict]


def group_by_project(rows: list[dict]) -> dict[str, list[dict]]:
    """Group by repo_name, not repo_owner/repo_name.

    CI-Repair-Bench rows are collected from benchmark-owned forks, so `agno`
    appears as RabeyaMuna/agno, Muna4029/agno and agno-agi/agno. Grouping by owner
    would split one project's history into three and inflate the project count
    from 103 to 139.
    """
    projects: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        projects[row["repo_name"]].append(row)
    for name in projects:
        projects[name].sort(key=lambda item: (item["commit_date"], item["id"]))
    return dict(projects)


def added_lines(diff: str, min_length: int = 15) -> set[str]:
    return {
        line[1:].strip()
        for line in (diff or "").splitlines()
        if line.startswith("+") and not line.startswith("+++") and len(line[1:].strip()) >= min_length
    }


_MEMORY_TEXT: dict[str, str] = {}


def memory_text(prior: dict) -> str:
    """Everything a memory item puts in front of the agent: its patch and its log.

    Overlap must be measured against the whole rendered block, not just the lines
    the patch adds. In axolotl, a line the target's gold patch introduces appears as
    an unchanged *context* line inside an earlier patch, so an added-lines-only
    check reports 0% overlap while the agent can still read the answer.
    """
    key = str(prior["id"])
    if key not in _MEMORY_TEXT:
        from .log_compressor import compress_logs

        _MEMORY_TEXT[key] = (prior["diff"] or "") + "\n" + compress_logs(prior["logs"])
    return _MEMORY_TEXT[key]


def patch_overlap(target: dict, prior: dict) -> float:
    """Fraction of the target's added lines that are visible in a memory item."""
    gold = added_lines(target["diff"])
    if not gold:
        return 0.0
    visible = memory_text(prior)
    return sum(1 for line in gold if line in visible) / len(gold)


def assert_no_leakage(target: dict, memory: list[dict], max_overlap: float = 0.25) -> None:
    for prior in memory:
        overlap = patch_overlap(target, prior)
        if overlap > max_overlap:
            raise LeakageError(
                f"memory item {prior['id']} reproduces {overlap:.0%} of the gold patch "
                f"for target {target['id']}"
            )
        if prior["id"] == target["id"]:
            raise LeakageError(f"memory contains the target instance {target['id']}")
        if prior["commit_date"] >= target["commit_date"]:
            raise LeakageError(
                f"memory item {prior['id']} is not strictly earlier than target {target['id']}"
            )
        if prior["sha_fail"] == target["sha_fail"] or prior["sha_success"] == target["sha_success"]:
            raise LeakageError(f"memory item {prior['id']} shares a commit with the target")
        if prior["diff"] == target["diff"]:
            raise LeakageError(f"memory item {prior['id']} has the target's gold patch")


def select_tasks(
    rows: list[dict],
    min_prior: int = 3,
    memory_size: int = 3,
    max_files: int = 3,
    semantic_only: bool = True,
    max_overlap: float = 0.25,
) -> list[Selection]:
    """Pick evaluation instances that have enough strictly earlier project history.

    The memory condition is only meaningful when the same project already failed CI
    before the target commit. Selection is chronological per project, which is the
    temporal split used by Learning to Commit and SWE-CI.

    `semantic_only` drops targets whose failure is purely formatting or linting.
    Those are 58% of CI-Repair-Bench, and their gold patch is often a whitespace
    edit, so they measure a formatter rather than repair ability.

    `max_overlap` drops memory items that reproduce the target's gold patch. Being
    strictly earlier is not sufficient: in taipy, instance 440 predates target 439
    by nine days yet already contains 97% of its fix, which would hand the answer to
    the memory condition and manufacture the effect the study is trying to measure.
    """
    selections: list[Selection] = []
    for _, history in group_by_project(rows).items():
        for target in history:
            if len(target["changed_files"] or []) > max_files:
                continue
            if semantic_only and not (set(target["error_type"] or []) & SEMANTIC_ERROR_TYPES):
                continue
            earlier = [
                item
                for item in history
                if item["commit_date"] < target["commit_date"]
                and item["diff"] != target["diff"]
                and item["sha_fail"] != target["sha_fail"]
                and patch_overlap(target, item) <= max_overlap
            ]
            if len(earlier) < min_prior:
                continue
            memory = earlier[-memory_size:]
            assert_no_leakage(target, memory, max_overlap=max_overlap)
            selections.append(Selection(target=target, memory=list(reversed(memory))))
    return selections
