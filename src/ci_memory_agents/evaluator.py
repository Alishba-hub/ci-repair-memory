from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path

COMMENT = re.compile(r"(^\s*#.*$)|(\s+#.*$)", re.MULTILINE)


@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    condition: str
    exact_match: bool
    normalized_match: bool
    file_iou: float
    file_precision: float
    file_recall: float
    file_f1: float
    line_deviation_ratio: float
    gold_line_delta: int
    predicted_line_delta: int
    files_matched: int
    files_expected: int
    partial_credit: float
    localized: bool
    touched_any_gold: bool
    gold_files: list[str]
    predicted_files: list[str]
    untouched_gold_files: list[str]
    collateral_files: list[str]
    file_checks: list[dict]

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_submission(
    submission_dir: Path,
    repo_before: Path,
    repo_after: Path,
    task_id: str = "",
    condition: str = "",
) -> EvaluationResult:
    """Score an agent submission against the gold post-fix snapshot.

    Scoring is deliberately not byte-exact. CI-Repair-Bench gold patches are whole
    commit diffs, so they routinely carry release notes and unrelated refactors
    alongside the real fix. File-level IoU and line deviation, as used by
    Learning to Commit, degrade gracefully where filecmp would report a flat zero.
    """
    gold_files = _changed_files(repo_before, repo_after)
    predicted_files = _changed_files(repo_before, submission_dir)

    intersection = gold_files & predicted_files
    union = gold_files | predicted_files
    file_iou = len(intersection) / len(union) if union else 1.0
    precision = len(intersection) / len(predicted_files) if predicted_files else 0.0
    recall = len(intersection) / len(gold_files) if gold_files else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # Per-file verdicts, so a run that fixed three of four gold files is visibly
    # different from one that fixed none. The pooled booleans below are just the
    # conjunction of these, kept for continuity with the earlier metric.
    checks = []
    for name in sorted(gold_files):
        submitted = _read(submission_dir / name)
        reference = _read(repo_after / name)
        checks.append(
            {
                "path": name,
                "touched": name in predicted_files,
                "exact": submitted == reference,
                "normalized": _normalize(submitted) == _normalize(reference),
                "gold_line_delta": _line_delta(repo_before / name, repo_after / name),
                "predicted_line_delta": _line_delta(repo_before / name, submission_dir / name),
            }
        )

    exact = bool(gold_files) and all(c["exact"] for c in checks)
    normalized = bool(gold_files) and all(c["normalized"] for c in checks)
    matched = sum(1 for c in checks if c["normalized"])

    gold_lines = sum(_line_delta(repo_before / name, repo_after / name) for name in gold_files)
    pred_lines = sum(_line_delta(repo_before / name, submission_dir / name) for name in predicted_files)
    deviation = abs(pred_lines - gold_lines) / gold_lines if gold_lines else float(pred_lines > 0)

    return EvaluationResult(
        task_id=task_id,
        condition=condition,
        exact_match=exact,
        normalized_match=normalized,
        file_iou=round(file_iou, 4),
        file_precision=round(precision, 4),
        file_recall=round(recall, 4),
        file_f1=round(f1, 4),
        line_deviation_ratio=round(deviation, 4),
        gold_line_delta=gold_lines,
        predicted_line_delta=pred_lines,
        files_matched=matched,
        files_expected=len(gold_files),
        partial_credit=round(matched / len(gold_files), 4) if gold_files else 0.0,
        localized=bool(gold_files) and recall == 1.0,
        touched_any_gold=bool(intersection),
        gold_files=sorted(gold_files),
        predicted_files=sorted(predicted_files),
        untouched_gold_files=sorted(gold_files - predicted_files),
        collateral_files=sorted(predicted_files - gold_files),
        file_checks=checks,
    )


def pass_at_k(total_runs: int, correct_runs: int, k: int) -> float:
    """Unbiased Pass@K estimator (Chen et al., used as the consistency metric).

    With n runs of which c succeeded, the chance that a sample of k contains at
    least one success is 1 - C(n-c, k) / C(n, k).
    """
    if k > total_runs:
        raise ValueError(f"k={k} exceeds the {total_runs} runs available")
    if total_runs - correct_runs < k:
        return 1.0
    return 1.0 - math.comb(total_runs - correct_runs, k) / math.comb(total_runs, k)


def _changed_files(baseline: Path, candidate: Path) -> set[str]:
    changed: set[str] = set()
    for path in _walk(baseline):
        if _read(baseline / path) != _read(candidate / path):
            changed.add(path)
    for path in _walk(candidate):
        if path not in changed and _read(baseline / path) != _read(candidate / path):
            changed.add(path)
    return changed


def _walk(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _read(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize(text: str | None) -> str | None:
    """Whitespace and comment insensitive form, mirroring CI-Bench's evaluate.sh."""
    if text is None:
        return None
    stripped = COMMENT.sub("", text)
    return "\n".join(line.rstrip() for line in stripped.splitlines() if line.strip())


def _line_delta(before: Path, after: Path) -> int:
    old = (_read(before) or "").splitlines()
    new = (_read(after) or "").splitlines()
    import difflib

    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    )
