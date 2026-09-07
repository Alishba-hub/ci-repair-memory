"""The one shape every executing oracle reports in.

Both backends -- pushing to GitHub Actions and running the distilled workflow in a
container -- have to answer the same question, and downstream code should not have to
know which one produced an answer. `source` records which did, because a paper that
mixes the two has to say how many instances came from each.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CIOutcome:
    """One CI re-execution.

    `conclusion` is "success", "failure", or "inconclusive". The third is not a failure:
    it means the workflow never delivered a verdict -- it could not be triggered, every
    job was skipped, or the harness could not run it here. CI-Repair-Bench folds those
    into failures when computing Pass@1, and `resolve` in the scorer does the same so the
    headline number stays comparable, but the distinction is kept on disk so a reviewer
    asking "how many of your failures are real failures" can be answered.
    """

    conclusion: str
    raw_conclusions: list[str] = field(default_factory=list)
    run_url: str = ""
    commit: str = ""
    branch: str = ""
    failed_jobs: list[str] = field(default_factory=list)
    detail: str = ""
    polled_seconds: int = 0
    source: str = ""
    steps: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.conclusion == "success"

    @property
    def decided(self) -> bool:
        return self.conclusion in ("success", "failure")

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def outcome_path(run_dir: Path) -> Path:
    return run_dir / "ci_outcome.json"


def read_outcome(path: Path) -> CIOutcome | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for derived in ("passed", "decided", "recorded_at"):
        payload.pop(derived, None)
    try:
        return CIOutcome(**payload)
    except TypeError:
        return None


def write_outcome(path: Path, outcome: CIOutcome) -> None:
    payload = outcome.as_dict()
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
