from __future__ import annotations

from pathlib import Path

from .design import K_VALUES, MEMORY_SIZE, RQ1_K
from .loader import Task


class InsufficientMemoryError(ValueError):
    """A task cannot fill the memory arm being asked of it.

    Separate from `ValueError` on the condition name because the two need different
    responses: an unknown arm is a bug in the caller, while a short memory list is a
    task that needs re-importing and that the layout step should report and skip.
    """


NO_MEMORY = "no_memory"


def memory_condition(k: int) -> str:
    """The arm that shows the agent `k` earlier failures."""
    return f"memory_k{k}"


#: The arms the harness lays out. One control plus one arm per K in the RQ3 sweep, so
#: RQ1 and RQ3 read off the same grid instead of needing two runs of the study.
CONDITIONS = (NO_MEMORY,) + tuple(memory_condition(k) for k in K_VALUES)

#: The single memory arm RQ1 reports against the control. Everything that computes "the
#: memory effect" -- the paired test, the discordant-pair counts, the delta on the
#: dashboard -- uses this pair and nothing else, because a paired test over four arms is
#: not defined and silently picking two of them is how an effect gets manufactured.
WITH_MEMORY = memory_condition(RQ1_K)
PAIR = (NO_MEMORY, WITH_MEMORY)

#: Arms that exist in `runs/` from before the K sweep, when there was one memory arm
#: called `with_memory`. Those runs used three memory items, which is exactly what
#: `memory_k3` means, but they are not renamed on disk: renaming a directory of
#: collected results to make it match a later vocabulary is how provenance is lost.
LEGACY_CONDITIONS = ("with_memory",)

#: `foreign_memory` shows the agent the same number of earlier failures, drawn from a
#: *different* project. It is the placebo arm: it separates "this project's history
#: helps" from "any worked example helps", which the RQ1 contrast on its own cannot.
#: It carries K items but not K items of project history, so it belongs to no point on
#: the RQ3 curve and is deliberately not a member of `CONDITIONS`.
CONTROL_ARMS = ("foreign_memory",)

#: Every arm that can appear under `runs/`: the design's own, the historical name, and
#: the placebo. Code that enumerates what is on disk uses this; code that enumerates the
#: design uses `CONDITIONS`. The distinction exists because the scorer once hardcoded
#: the pair and silently dropped completed runs from an arm it did not expect -- and
#: because a scorer that *raises* on an unexpected arm is just as bad, refusing to
#: report the runs someone spent credits collecting.
ALL_CONDITIONS = CONDITIONS + LEGACY_CONDITIONS + CONTROL_ARMS


def arm_kind(condition: str) -> str:
    """Why an arm exists: `design`, `legacy` (renamed), or `control` (placebo).

    Aggregations use this instead of testing arm names one at a time. RQ3 takes only
    `design` arms, because a dose-response curve must not include a dose that was
    inferred from an old name or that was never project history in the first place.
    """
    if condition in CONDITIONS:
        return "design"
    if condition in LEGACY_CONDITIONS:
        return "legacy"
    if condition in CONTROL_ARMS:
        return "control"
    raise ValueError(f"unknown condition {condition!r}, expected one of {ALL_CONDITIONS}")


def canonical_condition(condition: str) -> str:
    """The design arm a run should be analysed as.

    `with_memory` becomes `memory_k3`. The two are the same treatment under two names:
    the old arm rendered the three project memory items the task carried, which is what
    `memory_k3` means, so keeping them apart in an analysis would split one condition's
    evidence across two rows and leave both underpowered.

    The placebo arm maps to itself. It has no design equivalent -- its items were never
    this project's history -- and folding it into a memory arm would be the one merge
    that changes what is being measured.

    Provenance is not lost by this: `arm_kind` and the raw `condition` both travel in
    `runs.csv`, so a strict analysis can still exclude what was renamed.
    """
    return WITH_MEMORY if condition in LEGACY_CONDITIONS else condition


def condition_k(condition: str) -> int:
    """How many memory items an arm shows. 0 for the control.

    Raises for an unknown arm rather than defaulting to 0: a typo that reads as "no
    memory" would put runs from a real memory arm into the control's numbers.
    """
    kind = arm_kind(condition)
    if condition == NO_MEMORY:
        return 0
    if kind in ("legacy", "control"):
        return RQ1_K
    suffix = condition[len("memory_k"):]
    return int(suffix)


def condition_label(condition: str) -> str:
    """How an arm is named in tables and on the dashboard."""
    kind = arm_kind(condition)
    k = condition_k(condition)
    if k == 0:
        return "No memory"
    if kind == "legacy":
        return f"With memory (legacy, K={k})"
    if kind == "control":
        return f"Foreign memory (placebo, K={k})"
    return f"Memory K={k}"

_SHARED = (
    "You are fixing a continuous integration failure in this repository.\n"
    "Edit the repository files so the failing workflow passes.\n"
    "Test files may be edited when that is genuinely part of the fix, for example "
    "adding a skip for an unsupported platform or updating a test to a changed API. "
    "Do not delete or weaken assertions merely to hide a real defect.\n"
)

# Agents that only return text, such as Copilot through the VS Code language model API.
INSTRUCTIONS_TEXT = _SHARED + (
    "Return the complete new contents of every file you change, each preceded by "
    "its path on a line of the form `=== path/to/file ===`."
)

# Agents that edit the working directory themselves, such as Claude Code or Cursor.
INSTRUCTIONS_INPLACE = _SHARED + (
    "Apply your changes directly to the files in the current working directory. "
    "Do not only describe or print the fix: the files on disk must actually be "
    "modified, because only the files are evaluated. Finish by briefly listing "
    "which files you changed."
)

OUTPUT_MODES = {"text": INSTRUCTIONS_TEXT, "inplace": INSTRUCTIONS_INPLACE}

SCOPE_NOTE = (
    "Only the files listed under 'Repository files' are available to you, and only "
    "those may be edited. The log may mention other files; you are seeing an excerpt "
    "of a larger repository, and the fix belongs in the files shown above. Do not "
    "invent paths that are not listed."
)

# The note that replaces it once `repo_before` is a real checkout. It deliberately does
# not say where the fault is: locating it is part of the task, and the version of this
# harness that handed over the gold-patch file set was measuring repair with
# localization already done. See scripts/materialize_repos.py.
FULL_REPO_NOTE = (
    "This is the complete repository at the failing commit. Read whatever you need to "
    "diagnose the failure: the log names the workflow steps that failed, and the "
    "workflow definition above says what each step runs. Nothing has been pre-selected "
    "for you, so finding the responsible file is part of the task. Keep the change "
    "minimal and confined to what the failure requires."
)


def repo_scope(task: Task) -> str:
    """"full" once the task holds a real checkout, "focused" for the gold-files-only tree."""
    import json

    metadata = task.root / "metadata.json"
    if metadata.exists():
        try:
            return json.loads(metadata.read_text(encoding="utf-8")).get("repo_scope", "focused")
        except (json.JSONDecodeError, OSError):
            pass
    return "focused"


def build_prompt(
    task: Task,
    condition: str,
    file_budget: int = 12000,
    output_mode: str = "text",
    scope: str | None = None,
    tree_limit: int = 400,
) -> str:
    """Build the prompt for one experimental condition.

    Every condition receives the target failing log: it is the problem statement, and
    withholding it would leave no issue to solve. They differ only in how many earlier
    failures from this project are appended -- none for the control, `condition_k(...)`
    of them for a memory arm.

    The K arms take a *prefix* of the task's memory list, which is ordered most recent
    first. K=1 is therefore the first item of K=3 and K=3 the first three of K=5, so a
    difference between two arms is the additional history and not a different draw from
    it. Sampling each K independently would confound "more memory" with "different
    memory" and RQ3 would be unanswerable.

    Under the "focused" scope the handful of files are inlined, which is what the pilot
    did. Under "full" they cannot be, and should not be: a repository does not fit in a
    prompt, an agent with file tools reads what it needs, and which files it chooses to
    read is part of what the experiment measures. The prompt then carries a directory
    listing rather than contents.
    """
    if condition in CONTROL_ARMS:
        # The placebo arm's memory comes from another project, and which project that
        # was is recorded only in the prompt.md written at layout time. Rebuilding it
        # here would silently substitute this project's own history -- the exact
        # opposite of the arm's purpose -- so the on-disk prompt is the only valid
        # source. `dashboard_state.prompt_for` reads it before falling back here.
        raise ValueError(
            f"{condition!r} prompts cannot be rebuilt: the foreign memory they showed is "
            f"recorded only in the run's own prompt.md. Read that file instead."
        )
    if condition not in ALL_CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}, expected one of {ALL_CONDITIONS}")
    if output_mode not in OUTPUT_MODES:
        raise ValueError(f"unknown output_mode {output_mode!r}, expected one of {tuple(OUTPUT_MODES)}")

    scope = scope or repo_scope(task)
    sections = [
        f"# Task: {task.name}",
        f"## Repository\n{task.repo_name or task.task_id} at commit {task.sha_fail[:8] or 'local'}",
        f"## Failing CI log (the failure you must fix)\n```\n{(task.failing_log or task.issue).strip()}\n```",
    ]

    if scope == "full":
        sections.append(f"## Repository layout\n{_render_tree(task.repo_before, tree_limit)}")
        note = FULL_REPO_NOTE
    else:
        available = _list_files(task.repo_before)
        sections.append(
            "## Repository files\nThese are the only files you can edit:\n"
            + "\n".join(f"- {name}" for name in available)
            + "\n\n"
            + _render_files(task.repo_before, file_budget)
        )
        note = SCOPE_NOTE

    workflow = task.workflow.strip()
    if workflow:
        sections.insert(3, f"## Workflow definition\n```yaml\n{workflow[:2500]}\n```")

    k = condition_k(condition)
    if k:
        failures = "failure" if k == 1 else "failures"
        sections.append(
            f"## Repository memory: {k} earlier CI {failures} from this project, most recent first\n"
            "Each earlier failure below has its CI failure log and the golden patch the "
            "maintainers committed to fix it. They are precedent, not the answer to this task.\n\n"
            f"{_render_memory(task, k)}"
        )

    sections.append(f"## Instructions\n{OUTPUT_MODES[output_mode]}\n\n{note}")
    return "\n\n".join(sections)



def _render_tree(repo_root: Path, limit: int) -> str:
    """A directory listing, truncated by depth rather than alphabetically.

    A listing that ran out halfway through `a/` and never reached `z/` would
    systematically hide part of the repository from the agent, which is a bias and not
    merely a truncation. Shallow paths come first, so what survives the cap is a picture
    of the whole project rather than one corner of it.
    """
    if not repo_root.exists():
        return "_[repository not materialised; run scripts/materialize_repos.py]_"

    top = [
        f"{path.name}/" if path.is_dir() else path.name
        for path in sorted(repo_root.iterdir())
        if path.name != ".git"
    ]
    files = sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    shown = sorted(files, key=lambda name: (name.count("/"), name))[:limit]
    lines = [
        f"Top level: {', '.join(top)}",
        "",
        f"{len(files)} files in the repository. Listing {len(shown)}:",
    ]
    lines += [f"- {name}" for name in sorted(shown)]
    if len(files) > len(shown):
        lines.append(f"- ... and {len(files) - len(shown)} more; list them yourself as needed.")
    return "\n".join(lines)


def _list_files(repo_root: Path) -> list[str]:
    return [
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in sorted(repo_root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    ]


def _render_memory(task: Task, k: int) -> str:
    """The `k` most recent earlier failures plus the patches that resolved them.

    This is the agent-memory analogue of the retrieval step in CI-Repair-Bench:
    project-specific precedent rather than a similarity match over a global corpus.

    A task that cannot supply `k` items is refused rather than rendered short. Rendering
    three items into the K=5 arm would label a K=3 prompt as K=5, and RQ3 is precisely
    the comparison between those labels -- the one error that would not show up anywhere
    downstream, because every count and rate would still look complete.
    """
    if task.memory:
        if len(task.memory) < k:
            raise InsufficientMemoryError(
                f"task {task.task_id} has {len(task.memory)} memory items but condition "
                f"{memory_condition(k)} needs {k}. Re-import it with "
                f"scripts/import_ci_repair_bench.py, which stores {MEMORY_SIZE}."
            )
        # Headings number items without "of K" on purpose: item 1 then reads the same in
        # every arm, so the K=1 block stays a byte-exact prefix of K=3 and K=3 of K=5.
        blocks = []
        for order, item in enumerate(task.memory[:k], start=1):
            blocks.append(
                f"### Earlier failure {order} (instance {item.instance_id}, committed {item.commit_date})\n"
                f"Error type: {', '.join(item.error_type) or 'unknown'}\n"
                f"Files changed by the fix: {', '.join(item.changed_files) or 'unknown'}\n\n"
                f"#### Earlier failure {order}: CI failure log\n"
                f"```\n{_clip(item.log, MEMORY_ITEM_CHARS)}\n```\n\n"
                f"#### Earlier failure {order}: golden patch (the fix the maintainers committed)\n"
                f"```diff\n{_clip(item.diff, MEMORY_ITEM_CHARS)}\n```"
            )
        return "\n\n".join(blocks)

    legacy = task.historical_logs
    if not legacy:
        return "_No earlier failures recorded for this project._"
    return "\n\n".join(
        f"### {path.stem}\n```\n{path.read_text(encoding='utf-8').strip()}\n```" for path in legacy
    )


#: Per-item cap on an earlier failure's log and on its golden patch. The cut is marked in
#: the prompt: a patch that silently stops mid-hunk reads as the whole fix.
MEMORY_ITEM_CHARS = 2000


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... truncated: {len(text) - limit} more characters not shown]"


def _render_files(repo_root: Path, budget: int) -> str:
    paths = [p for p in sorted(repo_root.rglob("*")) if p.is_file() and ".git" not in p.parts]
    blocks: list[str] = []
    used = 0
    for number, path in enumerate(paths, start=1):
        relative = str(path.relative_to(repo_root)).replace("\\", "/")
        heading = f"### File {number} of {len(paths)}: {relative}"
        text = path.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > budget:
            blocks.append(f"{heading}\n=== {relative} ===\n[omitted: {len(text)} characters over prompt budget]")
            continue
        used += len(text)
        blocks.append(f"{heading}\n=== {relative} ===\n{text}")
    return "\n\n".join(blocks)
