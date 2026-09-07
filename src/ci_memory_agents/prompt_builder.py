from __future__ import annotations

from pathlib import Path

from .loader import Task

NO_MEMORY = "no_memory"
WITH_MEMORY = "with_memory"

CONDITIONS = (NO_MEMORY, WITH_MEMORY)

# Kept as an alias so that code which reads the arms present on disk keeps working. That
# code exists because the scorer once hardcoded the pair and silently dropped completed
# runs from an arm it did not expect; the lesson survives the arm that exposed it.
ALL_CONDITIONS = CONDITIONS

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

    Both conditions receive the target failing log: it is the problem statement, and
    withholding it would leave no issue to solve. They differ only in whether a block of
    three earlier failures from this project is appended.

    Under the "focused" scope the handful of files are inlined, which is what the pilot
    did. Under "full" they cannot be, and should not be: a repository does not fit in a
    prompt, an agent with file tools reads what it needs, and which files it chooses to
    read is part of what the experiment measures. The prompt then carries a directory
    listing rather than contents.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}, expected one of {CONDITIONS}")
    if output_mode not in OUTPUT_MODES:
        raise ValueError(f"unknown output_mode {output_mode!r}, expected one of {tuple(OUTPUT_MODES)}")

    scope = scope or repo_scope(task)
    sections = [
        f"# Task: {task.name}",
        f"## Repository\n{task.repo_name or task.task_id} at commit {task.sha_fail[:8] or 'local'}",
        f"## Failing CI log\n```\n{(task.failing_log or task.issue).strip()}\n```",
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

    if condition == WITH_MEMORY:
        sections.append(f"## Repository memory: earlier CI failures in this project\n{_render_memory(task)}")

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
