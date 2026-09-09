from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MemoryItem:
    instance_id: str
    commit_date: str
    error_type: list[str]
    changed_files: list[str]
    log_path: Path
    diff_path: Path

    @property
    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""

    @property
    def diff(self) -> str:
        return self.diff_path.read_text(encoding="utf-8") if self.diff_path.exists() else ""


@dataclass(frozen=True)
class Task:
    task_id: str
    name: str
    root: Path
    repo_before: Path
    repo_after: Path
    source: str
    target_files: list[str]
    error_type: list[str] = field(default_factory=list)
    commit_date: str = ""
    repo_name: str = ""
    sha_fail: str = ""
    #: Which of the three problem groups this task was selected under, as recorded at
    #: import time. Empty for tasks imported before the groups existed.
    error_group: str = ""
    #: How repo_after was produced by the importer: "git apply" is exact, anything else
    #: placed at least one hunk loosely. Empty for tasks imported before this was
    #: recorded, which is not the same as known-exact and is why it is not defaulted.
    patch_strategy: str = ""
    memory: list[MemoryItem] = field(default_factory=list)

    @property
    def memory_size(self) -> int:
        """The largest K this task can serve, since the arms take prefixes of `memory`."""
        return len(self.memory)

    def supports(self, k: int) -> bool:
        return k <= len(self.memory)

    @property
    def failing_log(self) -> str:
        for candidate in ("ci_logs/failed.compressed.log", "ci_logs/failed.log"):
            path = self.root / candidate
            if path.exists():
                return path.read_text(encoding="utf-8")
        return ""

    @property
    def workflow(self) -> str:
        path = self.root / "workflow.yml"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def issue(self) -> str:
        path = self.root / "issue.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def historical_logs(self) -> list[Path]:
        legacy = self.root / "historical_ci_logs"
        return sorted(legacy.rglob("*.log")) if legacy.exists() else []


def load_task(task_dir: Path) -> Task:
    metadata = json.loads((task_dir / "metadata.json").read_text(encoding="utf-8"))
    memory = [
        MemoryItem(
            instance_id=str(item["instance_id"]),
            commit_date=item.get("commit_date", ""),
            error_type=list(item.get("error_type") or []),
            changed_files=list(item.get("changed_files") or []),
            log_path=task_dir / item["log_file"],
            diff_path=task_dir / item["diff_file"],
        )
        for item in metadata.get("memory", [])
    ]
    return Task(
        task_id=metadata["task_id"],
        name=metadata["name"],
        root=task_dir,
        repo_before=task_dir / "repo_before",
        repo_after=task_dir / "repo_after",
        source=metadata.get("source", "local"),
        target_files=list(metadata.get("target_files") or []),
        error_type=list(metadata.get("error_type") or []),
        commit_date=metadata.get("commit_date", ""),
        repo_name=metadata.get("repo_name", ""),
        sha_fail=metadata.get("sha_fail", ""),
        error_group=metadata.get("error_group") or "",
        patch_strategy=metadata.get("patch_strategy", ""),
        memory=memory,
    )


def list_tasks(tasks_root: Path, source: str | None = None) -> list[Task]:
    tasks: list[Task] = []
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        if (task_dir / "metadata.json").exists():
            task = load_task(task_dir)
            if source is None or task.source == source:
                tasks.append(task)
    return tasks


#: Written by scripts/import_ci_repair_bench.py. See `study_task_ids`.
MANIFEST_NAME = "study_population.json"


def study_task_ids(tasks_root: Path) -> set[str] | None:
    """The task ids the current design declares, or None if nothing declares one.

    `tasks/` accumulates. A task imported under an earlier design keeps its folder, and
    the folder itself says nothing about which study it belongs to -- so every step that
    globbed `tasks/*` was really acting on "whatever has ever been imported here". That
    put pre-sweep tasks, which carry too few memory items to fill K=5, into the layout
    and into the fairness audit, where they read as failures of the study rather than as
    folders nobody selected.

    None means no manifest exists, and callers fall back to every folder on disk -- the
    right behaviour for a checkout that predates the manifest. A manifest that exists
    returns its set even when that set is empty, which is NOT the same thing: an import
    that produced nothing must leave the harness with nothing to run, not silently hand
    it every leftover from an earlier design. Collapsing the two was the first version
    of this function and it inverted the guarantee the manifest is for.

    A corrupt or unreadable manifest returns None rather than raising. It is a cache of
    a deterministic selection, not a source of truth, and losing it should degrade to
    the old behaviour rather than stop the harness.
    """
    manifest = tasks_root / MANIFEST_NAME
    if not manifest.exists():
        return None
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8"))["task_ids"]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return None
    if not isinstance(declared, list):
        return None
    return set(declared)


def list_study_tasks(tasks_root: Path, source: str | None = None) -> list[Task]:
    """`list_tasks`, narrowed to the declared design population when there is one."""
    tasks = list_tasks(tasks_root, source=source)
    declared = study_task_ids(tasks_root)
    if declared is None:
        return tasks
    return [task for task in tasks if task.task_id in declared]
