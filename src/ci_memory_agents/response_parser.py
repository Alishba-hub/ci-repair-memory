from __future__ import annotations

import re
from pathlib import Path

FILE_HEADER = re.compile(r"^===\s*(.+?)\s*===$")
FENCE = re.compile(r"^```")


def parse_files(reply: str) -> dict[str, str]:
    """Split an agent reply into {relative_path: contents}.

    The prompt asks for each changed file to be preceded by `=== path ===`.
    Models wrap the body in a markdown fence anyway, so fences are stripped.
    Mirrors parseFiles() in the VS Code extension so both paths behave identically.
    """
    files: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            files[current] = "\n".join(_strip_fences(buffer))

    for line in (reply or "").splitlines():
        header = FILE_HEADER.match(line)
        if header:
            flush()
            current = header.group(1).strip("`'\" ")
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    flush()
    return files


def _strip_fences(lines: list[str]) -> list[str]:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if body and FENCE.match(body[0].strip()):
        body.pop(0)
        if body and body[-1].strip() == "```":
            body.pop()
    return body


def write_files(workspace: Path, files: dict[str, str]) -> list[str]:
    """Write parsed files into a run workspace, refusing to escape it."""
    written: list[str] = []
    for relative, content in files.items():
        safe = relative.replace("\\", "/").lstrip("./")
        if ".." in Path(safe).parts or Path(safe).is_absolute():
            continue
        target = workspace / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        written.append(safe)
    return written
