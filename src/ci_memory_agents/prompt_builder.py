from __future__ import annotations

from pathlib import Path

from .loader import Task

NO_MEMORY = "no_memory"
WITH_MEMORY = "with_memory"
CONDITIONS = (NO_MEMORY, WITH_MEMORY)

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


def build_prompt(
    task: Task, condition: str, file_budget: int = 12000, output_mode: str = "text"
) -> str:
    """Build the prompt for one experimental condition.

    Both conditions receive the target failing log: it is the problem statement, and
    withholding it would leave no issue to solve. The conditions differ only in
    whether earlier failures from the same project are supplied as memory.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}, expected one of {CONDITIONS}")
    if output_mode not in OUTPUT_MODES:
        raise ValueError(f"unknown output_mode {output_mode!r}, expected one of {tuple(OUTPUT_MODES)}")

    available = _list_files(task.repo_before)
    sections = [
        f"# Task: {task.name}",
        f"## Repository\n{task.repo_name or task.task_id} at commit {task.sha_fail[:8] or 'local'}",
        f"## Failing CI log\n```\n{(task.failing_log or task.issue).strip()}\n```",
        f"## Repository files\nThese are the only files you can edit:\n"
        + "\n".join(f"- {name}" for name in available)
        + "\n\n"
        + _render_files(task.repo_before, file_budget),
    ]

    workflow = task.workflow.strip()
    if workflow:
        sections.insert(3, f"## Workflow definition\n```yaml\n{workflow[:2500]}\n```")

    if condition == WITH_MEMORY:
        sections.append(f"## Repository memory: earlier CI failures in this project\n{_render_memory(task)}")

    sections.append(f"## Instructions\n{OUTPUT_MODES[output_mode]}\n\n{SCOPE_NOTE}")
    return "\n\n".join(sections)


def _list_files(repo_root: Path) -> list[str]:
    return [
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in sorted(repo_root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    ]


def _render_memory(task: Task) -> str:
    """Earlier failures plus the patches that resolved them.

    This is the agent-memory analogue of the retrieval step in CI-Repair-Bench:
    project-specific precedent rather than a similarity match over a global corpus.
    """
    if task.memory:
        blocks = []
        for order, item in enumerate(task.memory, start=1):
            blocks.append(
                f"### Prior failure {order} (instance {item.instance_id}, {item.commit_date})\n"
                f"Error type: {', '.join(item.error_type) or 'unknown'}\n"
                f"Files changed by the fix: {', '.join(item.changed_files) or 'unknown'}\n\n"
                f"Log excerpt:\n```\n{item.log.strip()[:2000]}\n```\n\n"
                f"Patch that fixed it:\n```diff\n{item.diff.strip()[:2000]}\n```"
            )
        return "\n\n".join(blocks)

    legacy = task.historical_logs
    if not legacy:
        return "_No earlier failures recorded for this project._"
    return "\n\n".join(
        f"### {path.stem}\n```\n{path.read_text(encoding='utf-8').strip()}\n```" for path in legacy
    )


def _render_files(repo_root: Path, budget: int) -> str:
    blocks: list[str] = []
    used = 0
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = str(path.relative_to(repo_root)).replace("\\", "/")
        text = path.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > budget:
            blocks.append(f"=== {relative} ===\n[omitted: {len(text)} characters over prompt budget]")
            continue
        used += len(text)
        blocks.append(f"=== {relative} ===\n{text}")
    return "\n\n".join(blocks)
