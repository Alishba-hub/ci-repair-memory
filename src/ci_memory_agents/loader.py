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
    memory: list[MemoryItem] = field(default_factory=list)

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
