from __future__ import annotations

import re
from dataclasses import dataclass

TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

ERROR_ANCHORS = (
    "##[error]",
    "Traceback (most recent call last)",
    "Process completed with exit code",
    "FAILED ",
    "ERROR ",
    "error:",
    "Error:",
    "AssertionError",
    "ModuleNotFoundError",
    "ImportError",
    "SyntaxError",
    "would reformat",
    "Fatal error",
)

NOISE_GROUPS = (
    "Runner Image Provisioner",
    "Operating System",
    "Runner Image",
    "GITHUB_TOKEN Permissions",
    "Prepare all required actions",
    "Getting action download info",
    "Download immutable action package",
    "Environment variables",
    "Cleaning up orphan processes",
)

NOISE_LINES = (
    "##[group]",
    "##[endgroup]",
    "Download action repository",
    "Cache hit for:",
    "Received ",
    "Downloading ",
    "Collecting ",
    "Using cached ",
    "[command]/usr/bin/git",
    "##[command]/usr/bin/git",
    "[command]/usr/bin/docker",
    "##[command]/usr/bin/docker",
    "Temporarily overriding HOME",
    "Adding repository directory",
    "git version",
    "Stop and remove container",
    "Remove container network",
    "Print service container logs",
    "Node 20 is being deprecated",
    "Node.js 20 is deprecated",
)

TAIL_CUTOFF = (
    "Post job cleanup",
    "Cleaning up orphan processes",
    "Stop and remove container",
    "Remove container network",
)


@dataclass(frozen=True)
class CompressedLog:
    step_name: str
    text: str
    original_chars: int
    compressed_chars: int

    @property
    def ratio(self) -> float:
        if not self.original_chars:
            return 0.0
        return self.compressed_chars / self.original_chars


def compress_log(step_name: str, raw: str, budget: int = 3000, window: int = 12) -> CompressedLog:
    """Reduce a raw GitHub Actions job log to the region that explains the failure.

    Raw CI logs in CI-Repair-Bench have a median size of ~125K characters, which makes
    the memory condition impossible to express as a prompt. This keeps the lines around
    the failure anchors plus the tail of the log, inside a fixed character budget.
    """
    original_chars = len(raw or "")
    lines = _clean_lines(raw or "")
    if not lines:
        return CompressedLog(step_name, "", original_chars, 0)

    keep: set[int] = set()
    for index, line in enumerate(lines):
        if any(anchor in line for anchor in ERROR_ANCHORS):
            keep.update(range(max(0, index - window), min(len(lines), index + window + 1)))

    if not keep:
        keep.update(range(max(0, len(lines) - window * 2), len(lines)))
    keep.update(range(max(0, len(lines) - 5), len(lines)))

    text = _render(lines, sorted(keep))
    if len(text) > budget:
        text = _shrink(lines, sorted(keep), budget)
    return CompressedLog(step_name, text, original_chars, len(text))


def compress_logs(logs: list[dict], budget: int = 3000, max_steps: int = 3) -> str:
    """Compress the failing steps of one CI run into a single prompt-sized block."""
    compressed = [compress_log(item.get("step_name", "?"), item.get("log", "")) for item in logs or []]
    ranked = sorted(compressed, key=_failure_score, reverse=True)[:max_steps]
    blocks = []
    for item in ranked:
        if not item.text.strip():
            continue
        blocks.append(f"--- step: {item.step_name} ---\n{item.text}")
    joined = "\n\n".join(blocks)
    return joined[:budget] if len(joined) > budget else joined


def _failure_score(item: CompressedLog) -> int:
    return sum(item.text.count(anchor) for anchor in ERROR_ANCHORS)


def _clean_lines(raw: str) -> list[str]:
    cleaned: list[str] = []
    skipping = False
    for line in raw.splitlines():
        line = TIMESTAMP.sub("", line)
        line = ANSI.sub("", line).rstrip()
        if not line:
            continue
        if line.startswith("##[group]"):
            skipping = any(marker in line for marker in NOISE_GROUPS)
        if skipping:
            if line.startswith("##[endgroup]"):
                skipping = False
            continue
        if any(line.startswith(prefix) for prefix in NOISE_LINES):
            continue
        cleaned.append(line)
    return _trim_teardown(cleaned)


def _trim_teardown(lines: list[str]) -> list[str]:
    """Drop the runner teardown section, which never explains the failure."""
    for index in range(len(lines) - 1, -1, -1):
        if any(marker in lines[index] for marker in TAIL_CUTOFF):
            if index > len(lines) * 0.5:
                return lines[:index]
    return lines


def _render(lines: list[str], indices: list[int]) -> str:
    out: list[str] = []
    previous: int | None = None
    for index in indices:
        if previous is not None and index > previous + 1:
            out.append(f"... ({index - previous - 1} lines omitted)")
        out.append(lines[index])
        previous = index
    return "\n".join(out)


def _shrink(lines: list[str], indices: list[int], budget: int) -> str:
    """Keep the last anchors, which carry the actual failure, until the budget is used."""
    selected: list[int] = []
    size = 0
    for index in reversed(indices):
        cost = len(lines[index]) + 1
        if size + cost > budget:
            break
        selected.append(index)
        size += cost
    return _render(lines, sorted(selected))
