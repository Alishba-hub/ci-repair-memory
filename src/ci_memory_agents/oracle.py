"""One verdict per run, from the strongest oracle that could decide it.

The precedence is deliberate and is the whole argument of the evaluation section:

  1. static   -- deterministic, exact, and conclusive only in the negative direction.
                 An empty patch, a patch that does not parse, a deleted test. No model,
                 no execution, no ambiguity.
  2. execution-- the benchmark's own oracle: re-run the workflow, pass iff every check
                 passes. This is what CI-Repair-Bench reports Pass@1 against and it is
                 what our headline number should mean.
  3. judge    -- a model, sampled and voted, used only where 1 and 2 could not decide.

Every verdict carries the `oracle` that produced it, so a results table can state how
many of its successes were observed rather than inferred. A paper that reports a single
blended number without that breakdown is asking a reviewer to take the model's word for
it, which is exactly the objection raised in the meeting.

`resolve` also implements the benchmark's convention for instances the oracle could not
decide: CI-Repair-Bench counts an instance whose CI could not be triggered as an
unsuccessful repair. `strict_unresolved=True` follows that so the number is comparable;
`False` drops those runs instead, which is the more informative denominator for the
memory comparison. Report both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .ci_outcome import CIOutcome, outcome_path, read_outcome
from .judge import read_verdict
from .patchio import baseline_tree, unified_diff
from .static_checks import analyse

BASELINE_FILE = "ci_baseline.json"


@dataclass
class Verdict:
    """What we believe about one run, and on what evidence."""

    task_id: str
    condition: str
    run: str
    solved: bool
    oracle: str  # static | execution | judge | none
    decided: bool
    # False when no agent ever ran in this cell. The harness materialises a prompt and a
    # pristine workspace for every *planned* run, so an unattempted cell is
    # indistinguishable from an attempted one that changed nothing unless this is
    # checked. Scoring the two alike is what turned 158 executed runs into a 480-run
    # denominator and moved the reported repair rate from 49% to 17%.
    attempted: bool = True
    reason: str = ""
    cheats: bool = False
    mechanism: str = "none"
    confidence: str = "low"
    agreement: float = 1.0
    unstable: bool = False
    ci_conclusion: str = ""
    ci_run_url: str = ""
    judge_solved: bool | None = None
    static_verdict: str = ""
    error_type: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def run_attempted(run_dir: Path) -> bool:
    """Did an agent actually run in this cell?

    `auto_run` and the VS Code extension both write `agent_meta.json` only after the
    agent process has exited, and both refuse to overwrite one, so its presence is the
    harness's own record that the cell was executed. Its absence means the prompt and
    workspace were materialised and nothing more.

    This has to be asked before any patch-level check, because the deterministic
    "empty patch" test cannot tell the two apart: an unattempted cell holds a pristine
    copy of `repo_before`, which is exactly what an agent that declined to edit leaves
    behind. Treating the two alike counts every not-yet-run cell as a failed repair.
    """
    return (run_dir / "agent_meta.json").exists()


def baseline_path(task_root: Path) -> Path:
    """Where the unpatched run's outcome is cached, one per instance rather than per run."""
    return task_root / BASELINE_FILE


def read_baseline(task_root: Path) -> CIOutcome | None:
    return read_outcome(baseline_path(task_root))


GOLD_FILE = "ci_gold.json"


def gold_path(task_root: Path) -> Path:
    """Where the golden-patch run's outcome is cached: the failing commit plus only the
    maintainers' patch. Written by scripts/validate_instances.py and the dashboard."""
    return task_root / GOLD_FILE


def classify_instance(baseline: CIOutcome | None, gold: CIOutcome | None) -> str:
    """Why an instance is or is not a usable repair task, from its two reference builds."""
    if baseline is None or gold is None:
        return "unvalidated"
    if not baseline.decided:
        return "no-baseline"
    if baseline.passed:
        # Nothing to repair. With `deselect_tests` set this is also the guard against a
        # deselect list that swallowed the instance's own failure: exclude the target
        # test and the unpatched commit goes green, which would make every candidate
        # look like a repair. Rejecting here means the list is wrong, not the agent.
        return "already-green"
    if not gold.decided:
        return "gold-unknown"
    if not gold.passed:
        # The maintainer's own patch does not turn the workflow green here. Either the
        # standardization changed the semantics, or the failure needed something outside
        # the commit. Nothing an agent produces could be scored fairly on this instance.
        return "gold-red"
    return "usable"


def instance_is_usable(task_root: Path) -> tuple[bool, str]:
    """Whether an instance has been shown to fail before any repair is attempted.

    CI-Repair-Bench's Section 3.1 requires that the standardized workflow reproduce the
    original outcome, and excludes instances that do not. Their released harness never
    runs that check, so we run it: an instance whose workflow is already green at
    `sha_fail`, or which cannot be executed at all, proves nothing about repair and must
    not contribute to either arm. `scripts/validate_instances.py` records the answer.
    """
    outcome = read_baseline(task_root)
    if outcome is None:
        return False, "not validated: no baseline CI run recorded"
    if outcome.conclusion == "failure":
        return True, ""
    if outcome.conclusion == "success":
        return False, "the workflow already passes at sha_fail; nothing to repair"
    return False, f"baseline CI was inconclusive: {outcome.detail or 'no detail'}"


def resolve(
    run_dir: Path,
    task,
    condition: str,
    *,
    strict_unresolved: bool = True,
) -> Verdict:
    """Combine every piece of evidence about one run into a single verdict."""
    workspace = run_dir / "workspace"
    base = Verdict(
        task_id=task.task_id,
        condition=condition,
        run=run_dir.name,
        solved=False,
        oracle="none",
        decided=False,
        error_type=list(task.error_type),
    )

    if not workspace.exists():
        base.attempted = False
        base.reason = "no workspace on disk"
        base.solved = False if strict_unresolved else False
        return base

    if not run_attempted(run_dir):
        # Nothing was asked of an agent here yet. This is missing data, not a wrong
        # answer, and `strict_unresolved` must not fold it into failures the way it
        # folds an undecidable CI run: CI-Repair-Bench's convention covers instances
        # whose *repair attempt* could not be adjudicated, not cells that were never
        # dealt a repair attempt at all.
        base.attempted = False
        base.oracle = "none"
        base.decided = False
        base.reason = "no agent ran in this cell"
        return base

    if (run_dir / "agent_timeout.txt").exists():
        # The agent was interrupted mid-edit, so the files are a partial answer. Scoring
        # it as a wrong repair would report "ran out of time" as "got it wrong".
        base.reason = "the agent was killed by the timeout"
        base.oracle = "none"
        base.decided = False
        return base

    gold = task.root / "gold_patch.diff"
    gold_text = gold.read_text(encoding="utf-8") if gold.exists() else ""
    static = analyse(baseline_tree(task.root, workspace), workspace, gold_diff=gold_text)
    base.static_verdict = static.verdict

    judgement = read_verdict(run_dir) or {}
    if judgement:
        base.judge_solved = bool(judgement.get("solved"))

    if static.conclusive:
        base.solved = False
        base.oracle = "static"
        base.decided = True
        base.reason = static.reason
        base.cheats = bool(
            static.tests_removed
            or static.assertions_removed
            or static.unconditional_skips
            or static.workflow_weakened
        )
        base.confidence = "high"
        return base

    outcome = read_outcome(outcome_path(run_dir))
    if outcome is not None and outcome.decided:
        base.solved = outcome.passed
        base.oracle = "execution"
        base.decided = True
        base.confidence = "high"
        base.ci_conclusion = outcome.conclusion
        base.ci_run_url = outcome.run_url
        base.reason = outcome.detail or ("all checks passed" if outcome.passed else
                                         f"failed: {', '.join(outcome.failed_jobs[:2]) or 'CI red'}")
        return base

    if judgement:
        base.solved = bool(judgement.get("solved"))
        base.oracle = judgement.get("oracle", "judge")
        base.decided = True
        base.reason = str(judgement.get("reason", ""))
        base.cheats = bool(judgement.get("cheats"))
        base.mechanism = str(judgement.get("mechanism", "none"))
        base.confidence = str(judgement.get("confidence", "low"))
        base.agreement = float(judgement.get("agreement", 1.0))
        base.unstable = bool(judgement.get("unstable"))
        if outcome is not None:
            base.ci_conclusion = outcome.conclusion
            base.ci_run_url = outcome.run_url
        return base

    base.reason = "no oracle reached a verdict for this run"
    base.solved = False if strict_unresolved else False
    base.decided = False
    if outcome is not None:
        base.ci_conclusion = outcome.conclusion
        base.reason = f"CI inconclusive and no judgement: {outcome.detail}"
    return base


def candidate_diff(run_dir: Path, task) -> str:
    """The patch for this run, as something `git apply` will take."""
    workspace = run_dir / "workspace"
    return unified_diff(baseline_tree(task.root, workspace), workspace)


def load_verdicts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_verdicts(path: Path, verdicts: list[Verdict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for verdict in verdicts:
            handle.write(json.dumps(verdict.as_dict()) + "\n")
