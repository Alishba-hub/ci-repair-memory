from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from . import design


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


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def well_formed_diff(diff: str) -> bool:
    """Do the diff's hunk headers agree with the lines that follow them?

    62 of CI-Repair-Bench's 567 instances (11%) declare a hunk larger than the lines
    they actually carry -- the diffs are truncated in the published parquet. `git apply`
    rejects them with "corrupt patch at line N", so `repo_after` cannot be built and the
    instance has no gold state to score against.

    They are filtered here, at selection, rather than being discovered at import. An
    instance that fails to import consumes one of its repository's substitutes, and a
    repository with several of them runs out and ends up below its task quota -- which
    is how aiohttp came back with two tasks instead of three. Excluding them up front
    means the selector only ever proposes instances that can actually be materialised.

    Only the *target's* diff is checked. A memory item's patch is shown to the agent as
    text and never applied, so a truncated one is still a usable piece of history.
    """
    lines = (diff or "").splitlines()
    index = 0
    saw_hunk = False
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if not match:
            index += 1
            continue
        saw_hunk = True
        declared_old = int(match.group(1) or 1)
        declared_new = int(match.group(2) or 1)
        found_old = found_new = 0
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].startswith(("@@", "diff --git")):
            marker = lines[cursor][:1]
            if marker == "-":
                found_old += 1
            elif marker == "+":
                found_new += 1
            elif marker in (" ", ""):
                found_old += 1
                found_new += 1
            # A "\ No newline at end of file" marker counts toward neither side.
            cursor += 1
        if (found_old, found_new) != (declared_old, declared_new):
            return False
        index = cursor
    return saw_hunk


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
    min_prior: int = design.MIN_PRIOR,
    memory_size: int = design.MEMORY_SIZE,
    max_files: int = design.MAX_FILES,
    semantic_only: bool = design.SEMANTIC_ONLY,
    max_overlap: float = design.MAX_OVERLAP,
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
            if not well_formed_diff(target["diff"]):
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
            # Most recent first. The K arms take a prefix of this list, so K=1 is the
            # most recent failure, K=3 the three most recent, K=5 all five. Ordering it
            # oldest-first would make K=1 the least relevant item available and would
            # measure staleness rather than quantity of history.
            selections.append(Selection(target=target, memory=list(reversed(memory))))
    return selections


def select_study_tasks(
    rows: list[dict],
    n_repos: int = design.N_REPOS,
    tasks_per_repo: int = design.TASKS_PER_REPO,
    **filters,
) -> list[Selection]:
    """The study population: `n_repos` repositories x `tasks_per_repo` tasks each.

    Two constraints pull against each other. The design wants the 30 tasks spread over
    ten projects, so no single project's idiosyncrasies drive the result; it also wants
    them spread over the three problem groups, so the answer is not really an answer
    about dependency failures. They cannot both be satisfied exactly -- agno has no
    test-failure instances that survive the filters, browser-use and camel have nothing
    but dependency ones -- so repositories are chosen first and the group balance is
    optimised within that choice.

    Selection is deterministic. No sampling, no seed: the same parquet gives the same 30
    tasks on any machine, which is what makes a re-run a replication rather than a new
    draw.
    """
    eligible = select_tasks(rows, **filters)

    by_repo: dict[str, list[Selection]] = defaultdict(list)
    for selection in eligible:
        by_repo[selection.target["repo_name"]].append(selection)

    # Only repositories that can supply a full quota. A repository contributing one task
    # instead of three would make "per-repository" comparisons rest on single instances.
    candidates = {
        repo: items for repo, items in by_repo.items() if len(items) >= tasks_per_repo
    }
    if len(candidates) < n_repos:
        raise ValueError(
            f"only {len(candidates)} repositories have {tasks_per_repo} eligible targets, "
            f"need {n_repos}. Loosen design.MAX_FILES or lower design.MIN_PRIOR "
            f"(currently {design.MAX_FILES} and {design.MIN_PRIOR})."
        )

    def groups_of(repo: str) -> set[str]:
        return {
            g
            for g in (design.error_group(s.target["error_type"]) for s in candidates[repo])
            if g
        }

    # Prefer repositories that span more problem groups, then those with more to choose
    # from, then by name so ties never depend on dict ordering. Taking the largest
    # repositories instead would pick agno and conan first and hand most of the study to
    # two projects' error profiles.
    ranked = sorted(candidates, key=lambda r: (-len(groups_of(r)), -len(candidates[r]), r))
    chosen_repos = sorted(ranked[:n_repos])

    # Within the chosen repositories, fill the quota one task at a time, each time from
    # whichever problem group is currently most under-represented. Filling repository by
    # repository would let the first few exhaust their quota on one group before the
    # balance was ever considered.
    quota = {repo: tasks_per_repo for repo in chosen_repos}
    taken: dict[str, list[Selection]] = {repo: [] for repo in chosen_repos}
    counts: dict[str, int] = {name: 0 for name in design.GROUP_NAMES}
    remaining = {
        repo: sorted(candidates[repo], key=lambda s: (s.target["commit_date"], s.target["id"]))
        for repo in chosen_repos
    }

    for _ in range(n_repos * tasks_per_repo):
        best: tuple | None = None
        for repo in chosen_repos:
            if quota[repo] == 0:
                continue
            for index, selection in enumerate(remaining[repo]):
                group = design.error_group(selection.target["error_type"]) or ""
                # Rank by how starved this group is, then give repositories with fewer
                # spare instances first pick, so a repository with exactly three does
                # not have one of them taken by a repository that had alternatives.
                key = (
                    counts.get(group, 0),
                    len(remaining[repo]) - quota[repo],
                    repo,
                    str(selection.target["id"]),
                )
                if best is None or key < best[0]:
                    best = (key, repo, index, group)
        if best is None:
            break
        _, repo, index, group = best
        taken[repo].append(remaining[repo].pop(index))
        quota[repo] -= 1
        if group:
            counts[group] += 1

    return [selection for repo in chosen_repos for selection in taken[repo]]
