"""Turning what an agent did on disk into a patch, and checking that patch is usable.

Every oracle downstream of this module needs the same two things: the unified diff of
what the agent changed, and an answer to "would this apply to the real repository at
the failing commit". CI-Repair-Bench gates on the second before it spends a CI run
(`git apply --check --3way` in `benchmark_functions.fix_apply_generated_patch`), and
reports the count of applicable patches alongside Pass@1, because a framework that
emits 100 patches of which 30 apply is not the same as one that emits 100 that all do.

The agent's reply text is never used. An agent may describe edits it never made, so
only the files on disk are read.
"""

from __future__ import annotations

import difflib
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

# Files that are ours, not the repository's, and must never appear in a candidate
# patch: the prompt we handed the agent, the verdicts we wrote next to it, and the
# scratch some agents leave behind.
HARNESS_FILES = frozenset(
    {
        "prompt.md",
        "judgement.json",
        "verdict.json",
        "agent_timeout.txt",
        "agent_stdout.txt",
        "agent_stderr.txt",
        ".claude",
        ".cursor",
        ".github/copilot-instructions.md",
    }
)

EMPTY_DIFF = "[the agent changed no files]"


def _read(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _is_harness(name: str) -> bool:
    return name in HARNESS_FILES or any(
        name == prefix or name.startswith(prefix + "/") for prefix in HARNESS_FILES
    )


def walk(root: Path) -> set[str]:
    """Repository-relative paths under `root`, excluding git internals and our own files."""
    if not root.exists():
        return set()
    names = {
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    return {name for name in names if not _is_harness(name)}


def baseline_tree(task_root: Path, workspace: Path) -> Path:
    """The tree a run was actually given, which is not always the task's current one.

    `scripts/materialize_repos.py` replaces `repo_before` with a real checkout and keeps
    the old gold-files-only tree as `repo_before_focused`. Runs completed before that
    were copied from the focused tree, so diffing them against the new one would report
    every file of the repository as deleted and turn a whole pilot into noise.

    The workspace itself says which it came from: whichever candidate shares more of its
    file set is the one it was copied from. That is more reliable than a recorded flag,
    because the runs that need this predate any flag we could have written.
    """
    focused = task_root / "repo_before_focused"
    current = task_root / "repo_before"
    if not focused.exists():
        return current
    names = walk(workspace)
    if not names:
        return current
    return max(
        (current, focused),
        key=lambda tree: len(names & walk(tree)) - len(names ^ walk(tree)),
    )


def changed_files(baseline: Path, candidate: Path) -> set[str]:
    """Paths whose contents differ between the two trees, in either direction."""
    return {
        name
        for name in walk(baseline) | walk(candidate)
        if _read(baseline / name) != _read(candidate / name)
    }


def unified_diff(baseline: Path, candidate: Path, budget: int | None = None) -> str:
    """A git-applicable unified diff from `baseline` to `candidate`.

    `budget` truncates for prompt use and is announced in-band so a reader can see it
    is an excerpt. Leave it None for anything that will be applied: a truncated diff is
    not a patch.
    """
    blocks: list[str] = []
    used = 0
    for name in sorted(changed_files(baseline, candidate)):
        old, new = _read(baseline / name), _read(candidate / name)
        from_file = "/dev/null" if old is None else f"a/{name}"
        to_file = "/dev/null" if new is None else f"b/{name}"
        body = "".join(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=from_file,
                tofile=to_file,
                n=3,
            )
        )
        if not body:
            continue
        if not body.endswith("\n"):
            body += "\n\\ No newline at end of file\n"
        text = f"diff --git a/{name} b/{name}\n{body}"
        if budget is not None:
            if len(text) > budget // 2:
                text = text[: budget // 2] + "\n[... diff truncated for the prompt]\n"
            if used + len(text) > budget:
                blocks.append(f"diff --git a/{name} b/{name}\n[diff omitted: over the prompt budget]\n")
                continue
            used += len(text)
        blocks.append(text)
    return "".join(blocks) or (EMPTY_DIFF if budget is not None else "")


def workspace_diff(repo_before: Path, workspace: Path, budget: int = 24000) -> str:
    """Backwards-compatible prompt-shaped diff. New code should call `unified_diff`."""
    return unified_diff(repo_before, workspace, budget=budget)


@dataclass(frozen=True)
class Applicability:
    """Whether a candidate patch can be applied to the repository at the failing commit.

    Mirrors CI-Repair-Bench's gate: the patch must apply cleanly and may only touch
    files that exist at that commit. `checked` is False when no repository checkout was
    available to test against, which is not the same as a patch that failed the check
    and must not be reported as one.
    """

    checked: bool
    applies: bool
    touches_missing_files: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def check_applicable(diff: str, repo_checkout: Path | None) -> Applicability:
    """Dry-run the patch against a real checkout with `git apply --check --3way`."""
    if not (diff or "").strip():
        return Applicability(checked=True, applies=False, reason="empty patch")
    if repo_checkout is None or not (repo_checkout / ".git").exists():
        return Applicability(checked=False, applies=False, reason="no checkout to test against")

    tracked = set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=repo_checkout,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
    )
    missing = tuple(
        sorted(
            name
            for name in _paths_in_diff(diff)
            if name not in tracked and not (repo_checkout / name).exists()
        )
    )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".diff", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(diff if diff.endswith("\n") else diff + "\n")
        patch_path = handle.name
    try:
        result = subprocess.run(
            ["git", "apply", "--check", "--3way", "--whitespace=nowarn", patch_path],
            cwd=repo_checkout,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(patch_path).unlink(missing_ok=True)

    return Applicability(
        checked=True,
        applies=result.returncode == 0,
        touches_missing_files=missing,
        reason="" if result.returncode == 0 else result.stderr.strip()[:300],
    )


def apply_patch(diff: str, repo_checkout: Path) -> str | None:
    """Apply the patch for real. Returns None on success or a message on failure."""
    if not (diff or "").strip():
        return "empty patch"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".diff", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(diff if diff.endswith("\n") else diff + "\n")
        patch_path = handle.name
    try:
        result = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=nowarn", patch_path],
            cwd=repo_checkout,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(patch_path).unlink(missing_ok=True)
    return None if result.returncode == 0 else result.stderr.strip()[:300]


def _paths_in_diff(diff: str) -> list[str]:
    paths: list[str] = []
    for line in (diff or "").splitlines():
        if line.startswith("diff --git a/"):
            rest = line[len("diff --git a/") :]
            marker = rest.find(" b/")
            if marker != -1:
                name = rest[:marker]
                if name not in paths:
                    paths.append(name)
    return paths
