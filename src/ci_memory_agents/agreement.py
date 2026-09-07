"""How much the judge can be trusted, measured against the oracle that runs the code.

The objection raised in the meeting -- an LLM judge is non-deterministic and
hallucinates, so it cannot carry a result -- is correct as stated and is answerable, but
only with numbers. The answer is not "we improved the prompt". It is: on the subset of
runs where the workflow was actually re-executed, here is how often the judge agreed,
here is Cohen's kappa, and here is what it gets wrong in each direction.

Two failure directions matter differently and must be reported separately:

  false positive -- the judge says repaired, CI says red. This inflates the headline
                    rate and is the one a reviewer will press on.
  false negative -- the judge says not repaired, CI says green. This deflates it.

A judge whose errors are symmetric across the two arms biases the difference between
conditions less than its absolute rate suggests, which is worth stating when the effect
being measured is a difference. `by_condition` exists so that can be checked rather than
asserted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Agreement:
    """Judge against execution, over the runs where both delivered a verdict."""

    n: int
    both_solved: int
    both_failed: int
    judge_only: int  # judge says repaired, CI says red: a false positive
    execution_only: int  # judge says not repaired, CI says green: a false negative
    raw_agreement: float
    kappa: float
    precision: float
    recall: float
    f1: float
    judge_rate: float
    execution_rate: float
    rate_bias: float
    interpretation: str

    def as_dict(self) -> dict:
        return asdict(self)


def cohens_kappa(both: int, neither: int, only_a: int, only_b: int) -> float:
    """Chance-corrected agreement for two binary raters.

    Raw agreement alone is misleading here: when 85% of runs fail under both raters, a
    judge that answered "not repaired" every time would score 85% agreement and be
    worthless. Kappa is 0 for that judge.
    """
    n = both + neither + only_a + only_b
    if n == 0:
        return 0.0
    observed = (both + neither) / n
    a_yes, b_yes = (both + only_a) / n, (both + only_b) / n
    expected = a_yes * b_yes + (1 - a_yes) * (1 - b_yes)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return round((observed - expected) / (1 - expected), 4)


def _describe(kappa: float, n: int, judge_only: int, execution_only: int) -> str:
    if n < 20:
        return (
            f"Only {n} runs have both a judge verdict and a CI outcome. Report the count "
            f"and treat the agreement as indicative; 50 or more makes kappa stable enough "
            f"to quote."
        )
    if kappa >= 0.8:
        band = "almost perfect; the judge can stand in where CI could not be run"
    elif kappa >= 0.6:
        band = "substantial; usable as a secondary measure if the kappa is reported beside it"
    elif kappa >= 0.4:
        band = "moderate; not strong enough to carry a headline number on its own"
    else:
        band = "poor; do not report judge-derived rates as results"
    direction = ""
    if judge_only > 2 * max(execution_only, 1):
        direction = " Errors skew towards false positives, so judge-derived rates are optimistic."
    elif execution_only > 2 * max(judge_only, 1):
        direction = " Errors skew towards false negatives, so judge-derived rates are pessimistic."
    return f"Cohen's kappa {kappa:.2f} over {n} runs: {band}.{direction}"


def judge_vs_execution(rows: list[dict]) -> Agreement:
    """Compare the two oracles over runs that both decided.

    Each row needs `judge_solved` (bool or None) and `ci_conclusion`. Rows missing
    either are dropped, which is the honest denominator: a run CI never decided says
    nothing about the judge.
    """
    paired = [
        (bool(row["judge_solved"]), row["ci_conclusion"] == "success")
        for row in rows
        if row.get("judge_solved") is not None
        and row.get("ci_conclusion") in ("success", "failure")
    ]
    n = len(paired)
    if n == 0:
        return Agreement(
            0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            "No run has both a judge verdict and a CI outcome. Run the execution oracle "
            "on a sample of already-judged runs to produce this table.",
        )

    both = sum(1 for j, c in paired if j and c)
    neither = sum(1 for j, c in paired if not j and not c)
    judge_only = sum(1 for j, c in paired if j and not c)
    execution_only = sum(1 for j, c in paired if not j and c)

    precision = both / (both + judge_only) if (both + judge_only) else 0.0
    recall = both / (both + execution_only) if (both + execution_only) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    kappa = cohens_kappa(both, neither, judge_only, execution_only)
    judge_rate = (both + judge_only) / n
    execution_rate = (both + execution_only) / n

    return Agreement(
        n=n,
        both_solved=both,
        both_failed=neither,
        judge_only=judge_only,
        execution_only=execution_only,
        raw_agreement=round((both + neither) / n, 4),
        kappa=kappa,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        judge_rate=round(judge_rate, 4),
        execution_rate=round(execution_rate, 4),
        rate_bias=round(judge_rate - execution_rate, 4),
        interpretation=_describe(kappa, n, judge_only, execution_only),
    )


def by_condition(rows: list[dict]) -> dict[str, Agreement]:
    """The same comparison within each arm.

    A judge that is 10 points optimistic in both arms shifts both rates and leaves their
    difference intact. One that is optimistic only under `with_memory` manufactures the
    effect. This is the table that separates those two worlds, and it is the one worth
    putting in the paper.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("condition", "?"), []).append(row)
    return {condition: judge_vs_execution(group) for condition, group in sorted(groups.items())}


def binomial_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval, duplicated here so an agreement rate can carry one."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denominator
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)
