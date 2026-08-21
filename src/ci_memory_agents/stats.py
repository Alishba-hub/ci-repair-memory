from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Interval:
    low: float
    high: float

    def as_dict(self) -> dict:
        return asdict(self)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Interval:
    """95% Wilson score interval for a proportion.

    Preferred over the normal approximation here because the counts are small and
    the observed rate is often 0, where the normal interval collapses to zero width
    and would overstate confidence.
    """
    if total == 0:
        return Interval(0.0, 1.0)
    p = successes / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denominator
    return Interval(round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p-value for paired binary outcomes.

    `only_a` and `only_b` are the discordant counts: tasks solved under one condition
    and not the other. Concordant tasks carry no information about a difference and
    are correctly ignored. The exact binomial form is used rather than the chi-square
    approximation, which is invalid at these sample sizes.
    """
    n = only_a + only_b
    if n == 0:
        return 1.0
    smaller = min(only_a, only_b)
    tail = sum(math.comb(n, i) for i in range(smaller + 1)) / (2**n)
    return round(min(1.0, 2 * tail), 4)


def paired_bootstrap_difference(
    pairs: list[tuple[bool, bool]], iterations: int = 4000, seed: int = 0
) -> Interval:
    """95% percentile bootstrap interval for the with-minus-without difference.

    McNemar answers "is there a difference"; this answers "how large could it be".
    Tasks are resampled as whole pairs, which keeps the pairing that makes the
    comparison valid. Seeded, so the reported interval is reproducible.
    """
    import random

    if not pairs:
        return Interval(0.0, 0.0)
    rng = random.Random(seed)
    n = len(pairs)
    differences = []
    for _ in range(iterations):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        without = sum(1 for a, _ in sample if a) / n
        with_memory = sum(1 for _, b in sample if b) / n
        differences.append(with_memory - without)
    differences.sort()
    low = differences[int(0.025 * iterations)]
    high = differences[min(iterations - 1, int(0.975 * iterations))]
    return Interval(round(low, 4), round(high, 4))


def paired_analysis(per_task: dict[str, dict[str, bool]]) -> dict:
    """Compare two conditions over tasks measured in both.

    `per_task` maps task id to {"no_memory": solved, "with_memory": solved}.
    """
    paired = {
        task: outcome
        for task, outcome in per_task.items()
        if "no_memory" in outcome and "with_memory" in outcome
    }
    both = sum(1 for o in paired.values() if o["no_memory"] and o["with_memory"])
    neither = sum(1 for o in paired.values() if not o["no_memory"] and not o["with_memory"])
    only_no_memory = sum(1 for o in paired.values() if o["no_memory"] and not o["with_memory"])
    only_with_memory = sum(1 for o in paired.values() if o["with_memory"] and not o["no_memory"])

    n = len(paired)
    solved_no = both + only_no_memory
    solved_with = both + only_with_memory
    p_value = mcnemar_exact(only_no_memory, only_with_memory)

    return {
        "n_tasks": n,
        "both_solved": both,
        "neither_solved": neither,
        "only_no_memory": only_no_memory,
        "only_with_memory": only_with_memory,
        "discordant": only_no_memory + only_with_memory,
        "rate_no_memory": round(solved_no / n, 4) if n else None,
        "rate_with_memory": round(solved_with / n, 4) if n else None,
        "ci_no_memory": wilson_interval(solved_no, n).as_dict(),
        "ci_with_memory": wilson_interval(solved_with, n).as_dict(),
        "difference": round((solved_with - solved_no) / n, 4) if n else None,
        "ci_difference": paired_bootstrap_difference(
            [(o["no_memory"], o["with_memory"]) for o in paired.values()]
        ).as_dict(),
        "p_value": p_value,
        "significant": p_value < 0.05,
        "interpretation": _interpret(n, only_no_memory, only_with_memory, p_value),
    }


def _interpret(n: int, only_no_memory: int, only_with_memory: int, p_value: float) -> str:
    discordant = only_no_memory + only_with_memory
    if n < 10:
        return (
            f"Only {n} paired tasks. Too few to test; report as a pilot and state the "
            f"sample size as a limitation."
        )
    if discordant == 0:
        return (
            "The two conditions produced identical outcomes on every task. No effect is "
            "detectable, and no test is meaningful."
        )
    if discordant < 6:
        return (
            f"Only {discordant} tasks differ between conditions. Exact McNemar cannot reach "
            f"p<0.05 with fewer than 6 discordant pairs, so this is underpowered by "
            f"construction rather than merely non-significant."
        )
    if p_value < 0.05:
        direction = "with memory" if only_with_memory > only_no_memory else "without memory"
        return f"Difference favours {direction} and is significant (exact McNemar p={p_value})."
    return (
        f"No significant difference (exact McNemar p={p_value}). Report as no detected "
        f"effect, not as evidence of no effect."
    )


def minimum_discordant_for_significance(alpha: float = 0.05) -> int:
    """Smallest number of discordant pairs that could ever reach significance.

    Useful when writing up an underpowered pilot: below this, a null result says
    nothing about the hypothesis.
    """
    n = 1
    while n < 100:
        if mcnemar_exact(0, n) < alpha:
            return n
        n += 1
    return n
