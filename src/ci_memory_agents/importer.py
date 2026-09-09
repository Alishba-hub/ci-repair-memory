from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import design
from .log_compressor import compress_logs

RAW_URL = "https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"
DIFF_HEADER = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+?)$", re.MULTILINE)
UPSTREAM_OWNERS = {"RabeyaMuna", "Muna4029", "CI-Repair", "ci-benchmark-user"}


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def skip(self, task_id: str, reason: str) -> None:
        self.skipped.append((task_id, reason))


def diff_paths(diff: str) -> list[str]:
    """Paths a git diff touches, taken from the diff itself rather than changed_files."""
    paths: list[str] = []
    for match in DIFF_HEADER.finditer(diff or ""):
        for key in ("old", "new"):
            value = match.group(key)
            if value != "/dev/null" and value not in paths:
                paths.append(value)
    return paths


def fetch_file(owner: str, repo: str, sha: str, path: str, timeout: int = 30) -> str | None:
    url = RAW_URL.format(owner=owner, repo=repo, sha=sha, path=urllib.request.quote(path))
    request = urllib.request.Request(url, headers={"User-Agent": "ci-memory-agents/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def resolve_owner(row: dict) -> list[str]:
    """Benchmark rows point at benchmark-owned forks; try upstream owners too."""
    owner = row["repo_owner"]
    repo = row["repo_name"]
    candidates = [owner]
    if owner in UPSTREAM_OWNERS:
        candidates = [repo, f"{repo}-ai", owner]
    return candidates


def build_task(row: dict, memory_rows: list[dict], task_dir: Path, log_budget: int = 3000) -> str | None:
    """Materialise one CI-Repair-Bench row as a repo_before / repo_after task.

    Returns None on success, or a string explaining why the row was skipped.
    """
    paths = diff_paths(row["diff"])
    if not paths:
        return "diff has no file headers"

    repo = row["repo_name"]
    contents: dict[str, str | None] = {}
    for owner in resolve_owner(row):
        try:
            contents = {path: fetch_file(owner, repo, row["sha_fail"], path) for path in paths}
        except urllib.error.HTTPError as error:
            return f"http {error.code} for {owner}/{repo}"
        except Exception as error:  # network hiccup, rate limit
            return f"fetch failed: {type(error).__name__}"
        if any(value is not None for value in contents.values()):
            break
    if not any(value is not None for value in contents.values()):
        return f"no files retrievable at {row['sha_fail'][:8]}"

    repo_before = task_dir / "repo_before"
    repo_after = task_dir / "repo_after"
    for target in (repo_before, repo_after):
        if target.exists():
            shutil.rmtree(target)

    for path, text in contents.items():
        if text is None:
            continue
        destination = repo_before / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="")

    shutil.copytree(repo_before, repo_after)
    error = _apply_diff(repo_after, row["diff"])
    if error:
        shutil.rmtree(task_dir, ignore_errors=True)
        return error

    _write_task_files(row, memory_rows, task_dir, log_budget)
    return None


def _apply_diff(repo_after: Path, diff: str) -> str | None:
    subprocess.run(["git", "init", "-q"], cwd=repo_after, check=True, capture_output=True)
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8", newline="") as handle:
        handle.write(diff if diff.endswith("\n") else diff + "\n")
        patch_path = handle.name
    try:
        result = subprocess.run(
            ["git", "apply", "-p1", "--whitespace=nowarn", "--ignore-whitespace", patch_path],
            cwd=repo_after,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"git apply failed: {result.stderr.strip().splitlines()[:1]}"
    finally:
        Path(patch_path).unlink(missing_ok=True)
    shutil.rmtree(repo_after / ".git", ignore_errors=True, onexc=_force_remove)
    return None


def _force_remove(func, path, _exc) -> None:
    Path(path).chmod(0o700)
    func(path)


def _write_task_files(row: dict, memory_rows: list[dict], task_dir: Path, log_budget: int) -> None:
    (task_dir / "ci_logs").mkdir(parents=True, exist_ok=True)
    (task_dir / "memory").mkdir(parents=True, exist_ok=True)

    failing = compress_logs(row["logs"], budget=log_budget)
    (task_dir / "ci_logs" / "failed.compressed.log").write_text(failing, encoding="utf-8")
    (task_dir / "workflow.yml").write_text(row["workflow"] or "", encoding="utf-8")
    (task_dir / "gold_patch.diff").write_text(row["diff"], encoding="utf-8")

    memory_index = []
    for order, prior in enumerate(memory_rows):
        name = f"prior_{order:02d}_{prior['id']}"
        text = compress_logs(prior["logs"], budget=log_budget)
        (task_dir / "memory" / f"{name}.log").write_text(text, encoding="utf-8")
        (task_dir / "memory" / f"{name}.diff").write_text(prior["diff"], encoding="utf-8")
        memory_index.append(
            {
                "instance_id": prior["id"],
                "commit_date": prior["commit_date"],
                "error_type": list(prior["error_type"] or []),
                "workflow_filename": prior["workflow_filename"],
                "changed_files": list(prior["changed_files"] or []),
                "log_file": f"memory/{name}.log",
                "diff_file": f"memory/{name}.diff",
            }
        )

    metadata = {
        "task_id": task_dir.name,
        "name": f"CI failure in {row['repo_name']} ({', '.join(row['error_type'] or []) or 'unknown'})",
        "source": "ci-repair-bench",
        "instance_id": row["id"],
        "repo_name": row["repo_name"],
        "repo_owner": row["repo_owner"],
        "sha_fail": row["sha_fail"],
        "sha_success": row["sha_success"],
        "commit_date": row["commit_date"],
        "error_type": list(row["error_type"] or []),
        # Which of the three problem groups this task counts toward. Written at import
        # time rather than derived when reporting: the grouping is part of how the
        # population was chosen, so a later edit to `design.ERROR_GROUPS` must not
        # silently re-label tasks that were selected under the old one.
        "error_group": design.error_group(row["error_type"]),
        "workflow_path": row["workflow_path"],
        "target_files": diff_paths(row["diff"]),
        # The largest K this task can serve. The K arms take prefixes of `memory`, so
        # any arm above this number would be rendered short.
        "memory_size": len(memory_index),
        "memory": memory_index,
    }
    (task_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
