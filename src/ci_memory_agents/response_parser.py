from __future__ import annotations

import re
from pathlib import Path

FILE_HEADER = re.compile(r"^===\s*(.+?)\s*===$")
FENCE = re.compile(r"^```")
FENCE_OPEN = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")

# How much of the original a fenced block must cover before it is treated as the whole
# file rather than an excerpt. Deliberately generous: a real fix can legitimately shorten
# a file, but not to a tenth of it.
MIN_WHOLE_FILE_RATIO = 0.5


def parse_files(
    reply: str,
    known: list[str] | None = None,
    sizes: dict[str, int] | None = None,
) -> dict[str, str]:
    """Split an agent reply into {relative_path: contents}.

    The prompt asks for each changed file to be preceded by `=== path ===`, and when a
    model obeys that, this is unambiguous and nothing else is consulted.

    `known` is the list of paths the agent was allowed to edit. When supplied it enables
    a fallback for replies that carry the file but not the header -- prose, then a fenced
    block holding the whole file, with the path named in the surrounding sentence. That
    is a common and perfectly reasonable way for a model to answer, and treating it as
    "no files parsed" throws away a complete, correct submission over formatting. The
    fallback only ever writes to a path the task already listed, so it cannot invent
    files, and it never runs when explicit headers were found.

    `sizes` maps those paths to the original file's length. Supply it: models routinely
    quote a short excerpt of the change as well as the finished file, and a 116-character
    excerpt written over a 5,000-character test file does not read as "failed to parse",
    it reads as a deliberate deletion. The scorer would then record a repair attempt that
    gutted a test -- a failure this harness invented rather than observed.

    Mirrors parseFiles() in the VS Code extension so both paths behave identically.
    """
    files = _parse_headers(reply)
    if files or not known:
        return files
    return _parse_fenced(reply, known, sizes or {})


def _parse_headers(reply: str) -> dict[str, str]:
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


def _parse_fenced(reply: str, known: list[str], sizes: dict[str, int]) -> dict[str, str]:
    """Fenced code blocks, attributed to a path the task actually offers.

    Attribution rules, in order:
      1. the last known path mentioned in the text before the block;
      2. failing that, the task's only file, when it has exactly one.

    A block is then only accepted as a whole file if it is at least `MIN_WHOLE_FILE_RATIO`
    of the original's length. Anything shorter is an excerpt, and the run is reported as
    unparsed -- which is true, and which the scorer already handles -- rather than written
    out as a destructive edit.

    A block that cannot be attributed under either rule is dropped rather than guessed
    at. Writing a whole file to the wrong path would look like a repair and score as
    one, which is worse than parsing nothing.
    """
    lines = (reply or "").splitlines()
    paths = sorted(known, key=len, reverse=True)
    files: dict[str, str] = {}

    seen_path: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if not FENCE_OPEN.match(line.strip()):
            for path in paths:
                if path in line or Path(path).name in line:
                    seen_path = path
                    break
            index += 1
            continue

        body: list[str] = []
        index += 1
        while index < len(lines) and not FENCE.match(lines[index].strip()):
            body.append(lines[index])
            index += 1
        index += 1

        target = seen_path or (known[0] if len(known) == 1 else None)
        if target is None or not body:
            continue
        # A model that opens with ```python sometimes repeats the language as the first
        # line of the body. It is never valid source and would break the file it lands in.
        if body and body[0].strip() in ("python", "py", "yaml", "yml", "json", "toml", "text"):
            body = body[1:]
        # Keep the largest block seen for a path: models often show a small excerpt of
        # the change first and the complete file afterwards.
        candidate = "\n".join(body)
        if len(candidate) > len(files.get(target, "")):
            files[target] = candidate

    return {
        path: content
        for path, content in files.items()
        if not sizes.get(path) or len(content) >= sizes[path] * MIN_WHOLE_FILE_RATIO
    }


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
