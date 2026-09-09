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


def paired_analysis(
    per_task: dict[str, dict[str, bool]], pair: tuple[str, str] | None = None
) -> dict:
    """Compare two conditions over tasks measured in both.

    `per_task` maps task id to {<control arm>: solved, <memory arm>: solved}.

    `pair` names the two arms. It defaults to `prompt_builder.PAIR`, which is the RQ1
    contrast; pass an explicit pair to test any other two arms against each other, for
    instance K=1 against K=5. The arms used to be hardcoded here, which was correct
    while exactly two existed and became a silent bug the moment the K sweep added
    more: the memory arm is now `memory_k3`, and a lookup for `with_memory` would have
    found nothing and reported every task as unpaired.

    The output keys keep their historical `..._no_memory` / `..._with_memory` spelling
    whatever arms are passed. They name the *roles* in the contrast -- control and
    memory -- and the report, dashboard and LaTeX table all read them; renaming them to
    match the arm would rename a column in three places to say what `arms` already says.
    """
    from .prompt_builder import PAIR

    control, memory = pair or PAIR
    paired = {
        task: outcome
        for task, outcome in per_task.items()
        if control in outcome and memory in outcome
    }
    both = sum(1 for o in paired.values() if o[control] and o[memory])
    neither = sum(1 for o in paired.values() if not o[control] and not o[memory])
    only_no_memory = sum(1 for o in paired.values() if o[control] and not o[memory])
    only_with_memory = sum(1 for o in paired.values() if o[memory] and not o[control])

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
            [(o[control], o[memory]) for o in paired.values()]
        ).as_dict(),
        "p_value": p_value,
        "significant": p_value < 0.05,
        # Which two arms produced the numbers above. Without this the table is
        # ambiguous once more than two arms exist, and a reader cannot tell an RQ1
        # contrast from a K=1-against-K=5 one.
        "arms": {"control": control, "memory": memory},
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


# --------------------------------------------------------------------------------
# Run-level analysis, for the 5-episodes-per-task design
# --------------------------------------------------------------------------------


def paired_rate_analysis(
    per_task: dict[str, dict[str, list[bool]]],
    iterations: int = 10000,
    seed: int = 0,
    pair: tuple[str, str] | None = None,
) -> dict:
    """Compare conditions when each task was run several times.

    `paired_analysis` above collapses a task to one boolean, which is the right shape
    when there is one run per task and the wrong shape at five. Two things go wrong if
    it is used anyway. Collapsing with "solved at least once" throws away the difference
    between 1/5 and 5/5, which is most of what repeated runs were bought for. Pooling all
    runs as independent observations instead inflates the sample size by five and
    narrows every interval, because runs of the same task are not independent -- they
    share the bug, the repository and the log.

    So each task contributes its success *rate* under each condition, tasks are the unit
    of resampling, and the interval is a cluster bootstrap over tasks. This is the
    analysis that survives a reviewer asking what the effective sample size is: it is the
    number of tasks, and repeated runs reduce the noise in each task's estimate rather
    than adding observations.

    `per_task` maps task id to {<control arm>: [bool, ...], <memory arm>: [bool, ...]}.
    `pair` names the two arms and defaults to `prompt_builder.PAIR`, the RQ1 contrast.
    As in `paired_analysis`, the output keys keep their `..._no_memory` /
    `..._with_memory` spelling because they name the roles rather than the arms.
    """
    import random

    from .prompt_builder import PAIR

    control, memory = pair or PAIR
    paired = {
        task: outcome
        for task, outcome in per_task.items()
        if outcome.get(control) and outcome.get(memory)
    }
    if not paired:
        return {"n_tasks": 0, "interpretation": "No task was completed under both conditions."}

    rates = {
        task: {
            condition: sum(runs) / len(runs)
            for condition, runs in outcome.items()
            if runs
        }
        for task, outcome in paired.items()
    }
    differences = [r[memory] - r[control] for r in rates.values()]
    n = len(differences)

    mean_no = sum(r[control] for r in rates.values()) / n
    mean_with = sum(r[memory] for r in rates.values()) / n
    observed = mean_with - mean_no

    rng = random.Random(seed)
    task_ids = list(rates)
    samples = []
    for _ in range(iterations):
        drawn = [rates[task_ids[rng.randrange(n)]] for _ in range(n)]
        samples.append(
            sum(r[memory] for r in drawn) / n - sum(r[control] for r in drawn) / n
        )
    samples.sort()

    improved = sum(1 for d in differences if d > 0)
    worsened = sum(1 for d in differences if d < 0)
    unchanged = n - improved - worsened

    return {
        "n_tasks": n,
        "runs_per_task": round(
            sum(len(o[control]) + len(o[memory]) for o in paired.values()) / (2 * n), 2
        ),
        "rate_no_memory": round(mean_no, 4),
        "rate_with_memory": round(mean_with, 4),
        "difference": round(observed, 4),
        "ci_difference": Interval(
            round(samples[int(0.025 * iterations)], 4),
            round(samples[min(iterations - 1, int(0.975 * iterations))], 4),
        ).as_dict(),
        "tasks_improved": improved,
        "tasks_worsened": worsened,
        "tasks_unchanged": unchanged,
        "sign_test_p": mcnemar_exact(worsened, improved),
        "arms": {"control": control, "memory": memory},
        "per_task_difference": {task: round(r[memory] - r[control], 4)
                                for task, r in sorted(rates.items())},
        "interpretation": _interpret_rates(n, observed, samples, improved, worsened),
    }


def _interpret_rates(n: int, observed: float, samples: list[float], improved: int, worsened: int) -> str:
    low = samples[int(0.025 * len(samples))]
    high = samples[min(len(samples) - 1, int(0.975 * len(samples)))]
    if n < 10:
        return (
            f"{n} paired tasks is a pilot, not a study. The bootstrap interval "
            f"[{low:+.3f}, {high:+.3f}] is dominated by task-to-task variance; report the "
            f"per-task table instead of a pooled rate and say so explicitly."
        )
    if low > 0:
        return (
            f"Memory improves the repair rate by {observed:+.1%} "
            f"(95% CI [{low:+.1%}, {high:+.1%}], excludes zero) over {n} tasks; "
            f"{improved} improved, {worsened} got worse."
        )
    if high < 0:
        return (
            f"Memory *reduces* the repair rate by {observed:+.1%} "
            f"(95% CI [{low:+.1%}, {high:+.1%}], excludes zero) over {n} tasks."
        )
    return (
        f"Point estimate {observed:+.1%} but the 95% CI [{low:+.1%}, {high:+.1%}] contains "
        f"zero over {n} tasks. Report as no detected effect, and use "
        f"`tasks_for_power` to say how many tasks would be needed to detect it."
    )


def tasks_for_power(
    difference: float,
    per_task_sd: float,
    power: float = 0.8,
    alpha: float = 0.05,
) -> int:
    """Paired tasks needed to detect `difference` in per-task rates.

    Worth running before committing to a submission date. If the pilot's effect needs
    120 tasks and the budget covers 30, that is knowable now rather than after the
    experiments; and if it needs 25, the argument for expanding the task set is concrete
    rather than a reviewer's guess.

    Normal approximation to the paired t-test, which is close enough for planning and
    should be described as such rather than as an exact calculation.
    """
    if difference == 0 or per_task_sd <= 0:
        return 0
    z_alpha = 1.959963985  # two-sided 0.05
    z_beta = {0.8: 0.8416, 0.9: 1.2816, 0.95: 1.6449}.get(round(power, 2), 0.8416)
    n = ((z_alpha + z_beta) * per_task_sd / abs(difference)) ** 2
    return int(math.ceil(n))


def per_task_sd(per_task_difference: dict[str, float]) -> float:
    """Standard deviation of the per-task differences, the input `tasks_for_power` needs."""
    values = list(per_task_difference.values())
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
