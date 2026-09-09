from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .evaluator import evaluate_submission, pass_at_k
from .agreement import by_condition, judge_vs_execution
from .ci_outcome import outcome_path, read_outcome
from .judge import read_verdict
from .loader import list_tasks
from .prompt_builder import (
    ALL_CONDITIONS,
    CONDITIONS,
    PAIR,
    build_prompt,
    canonical_condition,
    condition_k,
    condition_label,
)

from .stats import minimum_discordant_for_significance, paired_analysis, wilson_interval

# `PAIR` is the two arms RQ1 compares, `CONDITIONS` the four the design lays out, and
# `ALL_CONDITIONS` everything that can appear on disk including the renamed and placebo
# arms. They were the same tuple while there were only two arms; keeping the three
# questions -- what is compared, what is planned, what exists -- separate is what stops
# an arm's completed runs from vanishing out of the scorer, which has happened here
# before.

# Per-run measures averaged per condition. Keeping one list means the summary, the
# CSV and the UI can never drift apart on what "the metrics" are.
RUN_METRICS = (
    "solved",
    "exact_match",
    "normalized_match",
    "partial_credit",
    "localized",
    "touched_any_gold",
    "file_recall",
    "file_precision",
    "file_f1",
    "file_iou",
    "line_deviation_ratio",
)


@dataclass(frozen=True)
class RunState:
    task_id: str
    condition: str
    run: str
    done: bool
    scored: dict | None


def _files(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_text(
            encoding="utf-8", errors="replace"
        )
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def is_edited(workspace: Path, repo_before: Path) -> bool:
    """A run counts as done once its workspace differs from the untouched snapshot."""
    return _files(workspace) != _files(repo_before)


def describe_design() -> dict:
    """The arm list and study targets, for the UI to render itself from.

    The dashboard used to hardcode `["no_memory", "with_memory"]` in nine places. Every
    one of them was a place the page could silently stop showing an arm that existed on
    disk -- which is the failure this project has already had twice, once in the scorer
    and once in the VS Code extension. Sending the design to the page means adding a K
    changes this function and nothing in the HTML.
    """
    from . import design

    return {
        "conditions": [
            {
                "id": condition,
                "label": condition_label(condition),
                "k": condition_k(condition),
                "kind": "design",
            }
            for condition in CONDITIONS
        ],
        "extra_conditions": [
            {
                "id": condition,
                "label": condition_label(condition),
                "k": condition_k(condition),
                "kind": "legacy" if canonical_condition(condition) != condition else "control",
                # The design arm this one is analysed as. Charts group by it; the task
                # list and run links keep the raw id, because that is the directory on
                # disk and folding it there would break navigation.
                "canonical": canonical_condition(condition),
            }
            for condition in ALL_CONDITIONS
            if condition not in CONDITIONS
        ],
        "pair": {"control": PAIR[0], "memory": PAIR[1]},
        "k_values": list(design.K_VALUES),
        "runs_per_condition": design.RUNS_PER_CONDITION,
        "n_tasks": design.N_TASKS,
        "n_repos": design.N_REPOS,
        "tasks_per_repo": design.TASKS_PER_REPO,
        "error_groups": list(design.GROUP_NAMES),
        "agents": [dict(agent) for agent in design.AGENTS],
        "planned_runs_per_agent": design.planned_runs_per_agent(),
    }


def collect(tasks_root: Path, runs_root: Path, agent: str) -> list[dict]:
    """Progress for every task and condition, cheap enough to poll."""
    agent_root = runs_root / agent
    payload = []
    for task in list_tasks(tasks_root):
        entry = {
            "task_id": task.task_id,
            "name": task.name,
            "repo": task.repo_name or task.task_id,
            "error_type": task.error_type,
            "source": task.source,
            "memory_count": len(task.memory),
            "conditions": {},
        }
        for condition in ALL_CONDITIONS:
            condition_dir = agent_root / task.task_id / condition
            runs = []
            if condition_dir.exists():
                for run_dir in sorted(condition_dir.iterdir()):
                    if not run_dir.is_dir():
                        continue
                    runs.append(
                        {
                            "run": run_dir.name,
                            "done": is_edited(run_dir / "workspace", task.repo_before),
                        }
                    )
            entry["conditions"][condition] = {
                "runs": runs,
                "done": sum(1 for r in runs if r["done"]),
                "total": len(runs),
            }
        if any(entry["conditions"][c]["total"] for c in ALL_CONDITIONS):
            payload.append(entry)
    return payload


def telemetry(run_dir: Path) -> dict:
    """What the runner recorded about the run itself, as opposed to its answer.

    Everything here is optional: runs done by hand through the dashboard have no
    `agent_meta.json`, and must still appear in the tables.
    """
    meta: dict = {}
    path = run_dir / "agent_meta.json"
    if path.exists():
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    duration = meta.get("duration_ms")
    return {
        "duration_ms": duration,
        "duration_s": round(duration / 1000, 1) if isinstance(duration, (int, float)) else None,
        "exit_code": meta.get("exit_code"),
        "prompt_chars": meta.get("prompt_chars"),
        "reply_chars": meta.get("reply_chars"),
        "files_written": meta.get("files_written") or [],
        "finished_at": meta.get("finished_at"),
        "mode": meta.get("mode"),
        "automated": bool(meta),
        "has_response": (run_dir / "agent_response.md").exists(),
        "had_stderr": (run_dir / "agent_stderr.txt").exists(),
        "timed_out": (run_dir / "agent_timeout.txt").exists(),
    }


def prompt_for(tasks_root: Path, runs_root: Path, agent: str, task_id: str, condition: str, run: str) -> str:
    path = runs_root / agent / task_id / condition / run / "prompt.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    task = next(t for t in list_tasks(tasks_root) if t.task_id == task_id)
    return build_prompt(task, condition)


def _verdict_fields(run_dir: Path) -> dict:
    """Fold the judge's verdict into a run's row.

    `solved` is the reported success criterion, and it is the judge's answer whenever
    there is one: whether the run removed the same root cause as the maintainer's patch.
    `normalized_match` asks the different question of whether the run reproduced the
    maintainer's *text*, which a valid repair written another way fails by definition.

    An unjudged run is not solved. It is deliberately not fallen back to
    `normalized_match`: mixing two criteria in one column would make the rate depend on
    how far the judging got. `judged` carries that distinction so the summary can say
    how much of the experiment has actually been graded.
    """
    # The executed outcome, when there is one. It is carried alongside the judge's
    # verdict rather than replacing it, because the interesting quantity is the pair:
    # a judge that says "fixed" where CI says red is the finding, and collapsing the
    # two into one column would hide exactly that.
    outcome = read_outcome(outcome_path(run_dir))
    ci = None
    if outcome is not None:
        ci = {
            "conclusion": outcome.conclusion,
            "passed": outcome.passed,
            "decided": outcome.decided,
            "url": outcome.run_url,
            "detail": outcome.detail,
        }

    verdict = read_verdict(run_dir)
    if verdict is None:
        return {"solved": False, "judged": False, "judge": None, "ci": ci, "disagrees": False}
    judged_solved = bool(verdict.get("solved"))
    return {
        # Execution decides when it ran. That is the benchmark's own oracle and the
        # judge is a stand-in for it, so where both exist the stand-in does not win.
        "solved": ci["passed"] if (ci and ci["decided"]) else judged_solved,
        "judged": True,
        "judge_solved": judged_solved,
        "judge": {
            "fixes_failure": verdict.get("fixes_failure"),
            "cheats": verdict.get("cheats"),
            "mechanism": verdict.get("mechanism"),
            "confidence": verdict.get("confidence"),
            "reason": verdict.get("reason", ""),
        },
        "ci": ci,
        "disagrees": bool(ci and ci["decided"] and ci["passed"] != judged_solved),
    }


_SCORE_CACHE: dict[tuple, tuple[tuple, dict]] = {}
_CACHE_EPOCH = 0


def invalidate_scores() -> None:
    """Drop cached scores after something on disk was changed from the dashboard."""
    global _CACHE_EPOCH
    _CACHE_EPOCH += 1


def _score_fingerprint(agent_root: Path, task_id: str | None) -> tuple:
    """Cheap signature of everything `score_task` reads.

    Two `stat` calls per run instead of re-reading three repository trees: the runner
    rewrites `agent_meta.json` when a run finishes and the judge rewrites
    `judgement.json` when it grades one, so any change that can move a number also
    moves an mtime here. The workspace directory's own mtime catches files being added
    or removed by hand; edits made from the dashboard bump the epoch instead.
    """
    parts: list = [_CACHE_EPOCH]
    if not agent_root.exists():
        return tuple(parts)
    for task_dir in sorted(agent_root.iterdir()):
        if task_id and task_dir.name != task_id:
            continue
        for condition in ALL_CONDITIONS:
            condition_dir = task_dir / condition
            if not condition_dir.exists():
                continue
            for run_dir in sorted(condition_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                # `ci_outcome.json` belongs here for the same reason the other two do:
                # executing the workflow changes what `solved` means for that run, and a
                # signature that misses it serves a cached page saying "not run" over
                # results that exist on disk.
                for name in (
                    "agent_meta.json",
                    "judgement.json",
                    "ci_outcome.json",
                    "workspace",
                    "agent_timeout.txt",
                ):
                    path = run_dir / name
                    try:
                        parts.append((str(path), path.stat().st_mtime_ns))
                    except OSError:
                        parts.append((str(path), None))
    return tuple(parts)


def score_task(tasks_root: Path, runs_root: Path, agent: str, task_id: str | None) -> dict:
    """Score completed workspaces and summarise both conditions.

    Cached on a fingerprint of the run folders. Scoring reads every workspace and both
    repository snapshots for all 480 runs, which takes tens of seconds; the results
    view, the CSV export and the LaTeX export each ask for exactly the same thing, and
    without this a download appears to hang while the work is redone from scratch.
    """
    agent_root = runs_root / agent
    key = (str(tasks_root), str(runs_root), agent, task_id)
    fingerprint = _score_fingerprint(agent_root, task_id)
    cached = _SCORE_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    data = _score_task_uncached(tasks_root, runs_root, agent, task_id)
    _SCORE_CACHE[key] = (fingerprint, data)
    return data


def _score_task_uncached(tasks_root: Path, runs_root: Path, agent: str, task_id: str | None) -> dict:
    tasks = {t.task_id: t for t in list_tasks(tasks_root)}
    agent_root = runs_root / agent
    results = []
    timed_out: list[dict] = []
    no_edit: list[dict] = []
    planned = 0
    for task_dir in sorted(agent_root.iterdir()) if agent_root.exists() else []:
        if task_id and task_dir.name != task_id:
            continue
        task = tasks.get(task_dir.name)
        if task is None:
            continue
        for condition in ALL_CONDITIONS:
            condition_dir = task_dir / condition
            if not condition_dir.exists():
                continue
            for run_dir in sorted(condition_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                planned += 1
                workspace = run_dir / "workspace"
                stamp = telemetry(run_dir)
                label = {
                    "task_id": task.task_id,
                    "condition": condition,
                    "run": run_dir.name,
                    "duration_s": stamp["duration_s"],
                }
                edited = workspace.exists() and is_edited(workspace, task.repo_before)
                # A run killed by the timeout was interrupted mid-edit, so its files are
                # a partial answer. Scoring it would mix "got it wrong" with "ran out of
                # time", which are different failures.
                if stamp["timed_out"]:
                    timed_out.append(label)
                    continue
                if not edited:
                    # Finished but changed nothing on disk: a real failure mode worth
                    # counting separately from a wrong edit, and invisible if dropped.
                    if stamp["automated"]:
                        no_edit.append(label)
                    continue
                result = evaluate_submission(
                    workspace, task.repo_before, task.repo_after, task.task_id, condition
                )
                payload = result.as_dict()
                payload["run"] = run_dir.name
                payload["error_type"] = task.error_type
                payload["repo"] = task.repo_name or task.task_id
                payload["memory_count"] = len(task.memory)
                payload["telemetry"] = stamp
                payload.update(_verdict_fields(run_dir))
                results.append(payload)
    summary = summarize(results)
    summary["timed_out"] = timed_out
    summary["no_edit"] = no_edit
    summary["coverage"] = {
        "planned": planned,
        "scored": len(results),
        "timed_out": len(timed_out),
        "no_edit": len(no_edit),
        "not_started": planned - len(results) - len(timed_out) - len(no_edit),
        # An unjudged run counts as unsolved, so a partly judged experiment reads as
        # worse than it is. Surfacing the count keeps that visible rather than implicit.
        "judged": sum(1 for r in results if r.get("judged")),
        "unjudged": sum(1 for r in results if not r.get("judged")),
        # How many verdicts were observed rather than inferred. Kept beside the judged
        # count because they answer different questions: "how much has been graded" and
        # "how much of that grading can be trusted".
        "executed": sum(1 for r in results if (r.get("ci") or {}).get("decided")),
        "disagreements": sum(1 for r in results if r.get("disagrees")),
    }
    summary["agreement"] = _agreement_panel(results)
    return {"results": results, "summary": summary}


def _agreement_panel(results: list[dict]) -> dict:
    """Cohen's kappa between the judge and real CI, for the dashboard header.

    This is the number that decides how much any other number on the page is worth. It
    belongs in the summary rather than a sub-page: a repair rate produced entirely by a
    model, next to a kappa saying that model tracks reality no better than a coin, is
    one claim; the same rate with kappa 0.8 is a different claim, and a reader should
    not have to go looking to tell which one they are reading.
    """
    rows = [
        {
            "condition": r["condition"],
            "judge_solved": r.get("judge_solved", r["solved"]),
            "ci_conclusion": (r.get("ci") or {}).get("conclusion", ""),
        }
        for r in results
        if r.get("judged") and (r.get("ci") or {}).get("decided")
    ]
    if not rows:
        return {"n": 0, "ready": False}

    overall = judge_vs_execution(rows)
    return {
        "n": overall.n,
        "ready": overall.n >= 50,
        "kappa": round(overall.kappa, 3),
        "raw_agreement": round(overall.raw_agreement, 3),
        "judge_rate": round(overall.judge_rate, 3),
        "execution_rate": round(overall.execution_rate, 3),
        "rate_bias": round(overall.rate_bias, 3),
        "false_positives": overall.judge_only,
        "false_negatives": overall.execution_only,
        "interpretation": overall.interpretation,
        "by_condition": {
            name: {
                "n": a.n,
                "kappa": round(a.kappa, 3),
                "judge_rate": round(a.judge_rate, 3),
                "execution_rate": round(a.execution_rate, 3),
                "rate_bias": round(a.rate_bias, 3),
            }
            for name, a in by_condition(rows).items()
        },
    }


def summarize(results: list[dict], k: int = 1) -> dict:
    """Per-condition means plus Pass@K, computed only over runs that were attempted."""
    summary: dict = {"conditions": {}, "attempted": len(results)}
    for condition in ALL_CONDITIONS:
        rows = [r for r in results if canonical_condition(r["condition"]) == condition]
        if not rows:
            summary["conditions"][condition] = None
            continue
        solved = sum(1 for r in rows if r["solved"])
        entry = {
            "runs": len(rows),
            "solved": solved,
            # Kept under their historical names so older callers keep working.
            "exact": sum(1 for r in rows if r["exact_match"]) / len(rows),
            "normalized": solved / len(rows),
            "iou": sum(r["file_iou"] for r in rows) / len(rows),
            "recall": sum(r["file_recall"] for r in rows) / len(rows),
            "run_rate_ci": wilson_interval(solved, len(rows)).as_dict(),
        }
        for metric in RUN_METRICS:
            entry[f"mean_{metric}"] = round(
                sum(float(r[metric]) for r in rows) / len(rows), 4
            )
        # One run that rewrote a file the real fix barely touched drags the mean ratio
        # into the hundreds, so the typical run needs the median beside it.
        deviations = sorted(r["line_deviation_ratio"] for r in rows)
        entry["median_line_deviation_ratio"] = deviations[len(deviations) // 2]
        entry["telemetry"] = _telemetry_summary(rows)
        summary["conditions"][condition] = entry

    # Keyed on the *canonical* arm, so runs collected under the old `with_memory` name
    # count toward `memory_k3`, which is the same treatment. The per-condition table
    # above stays keyed on the raw arm, because a reader looking at coverage wants to
    # see what is actually on disk; only the comparisons fold the two together.
    by_task: dict[tuple[str, str], list[dict]] = {}
    for row in results:
        by_task.setdefault((row["task_id"], canonical_condition(row["condition"])), []).append(row)

    # Compare only tasks finished in BOTH arms of the contrast. A half-finished task
    # would otherwise contribute its score to one side and nothing to the other, which
    # invents a memory effect out of scheduling order rather than out of the data.
    control, memory_arm = PAIR
    task_ids = {task for task, _ in by_task}
    paired = sorted(
        task for task in task_ids
        if (task, control) in by_task and (task, memory_arm) in by_task
    )
    summary["paired_tasks"] = len(paired)
    summary["unpaired_tasks"] = sorted(task_ids - set(paired))

    scores: dict[str, list[float]] = {}
    for task in paired:
        for condition in ALL_CONDITIONS:
            # A task is "paired" on the two comparison arms; the control need not exist.
            # Indexing it unconditionally assumed every arm was present for every task,
            # which was true only while there were exactly two.
            rows = by_task.get((task, condition), [])
            correct = sum(1 for r in rows if r["solved"])
            if len(rows) >= k:
                scores.setdefault(condition, []).append(pass_at_k(len(rows), correct, k))
    summary["pass_at_k"] = {c: sum(v) / len(v) for c, v in scores.items()}
    summary["k"] = k

    # Per task success = solved in at least one run of that condition. This binarisation
    # is what makes the paired McNemar test applicable.
    solved: dict[str, dict[str, bool]] = {}
    for (task, condition), rows in by_task.items():
        solved.setdefault(task, {})[condition] = any(r["solved"] for r in rows)
    summary["analysis"] = paired_analysis(solved)
    summary["min_discordant_for_significance"] = minimum_discordant_for_significance()
    summary["by_error_type"] = _by_error_type(results)
    summary["by_task"] = _by_task_table(results, paired)
    summary["pass_at_k_curve"] = _pass_at_k_curve(by_task, paired)
    summary["funnel"] = _funnel(results)
    summary["recall_histogram"] = _histogram(results, "file_recall")

    # RQ1's headline number, always the same named contrast. The previous rule -- "if
    # exactly two arms are present" -- silently became wrong once four were: with the K
    # sweep it would either report nothing, or report whichever two arms happened to
    # have data as though they were the memory effect.
    summary["pair"] = {"control": control, "memory": memory_arm}
    if control in summary["pass_at_k"] and memory_arm in summary["pass_at_k"] and paired:
        summary["memory_effect"] = (
            summary["pass_at_k"][memory_arm] - summary["pass_at_k"][control]
        )
    else:
        summary["memory_effect"] = None

    # RQ3: Pass@K at every memory size, ordered by K, for the dose-response chart. Only
    # design arms -- the placebo's items were never this project's history, so it is not
    # a point on this curve, though it stays in the per-condition table above.
    summary["memory_size_curve"] = [
        {
            "condition": condition,
            "k": condition_k(condition),
            "label": condition_label(condition),
            "pass_at_k": round(summary["pass_at_k"][condition], 4),
            "tasks": len(scores[condition]),
        }
        for condition in sorted(summary["pass_at_k"], key=condition_k)
        if condition in CONDITIONS
    ]
    return summary


def _telemetry_summary(rows: list[dict]) -> dict:
    """Wall clock and prompt size per condition.

    The memory condition carries a much longer prompt, so any difference in success
    has to be read against a difference in cost; reporting one without the other
    would make memory look free.
    """
    def mean(key: str) -> float | None:
        values = [r["telemetry"][key] for r in rows if r["telemetry"].get(key) is not None]
        return round(sum(values) / len(values), 1) if values else None

    durations = sorted(
        r["telemetry"]["duration_s"] for r in rows if r["telemetry"].get("duration_s") is not None
    )
    solved_times = [
        r["telemetry"]["duration_s"]
        for r in rows
        if r["solved"] and r["telemetry"].get("duration_s") is not None
    ]
    return {
        "mean_duration_s": mean("duration_s"),
        "median_duration_s": durations[len(durations) // 2] if durations else None,
        "total_duration_s": round(sum(durations), 1) if durations else None,
        "mean_prompt_chars": mean("prompt_chars"),
        "mean_reply_chars": mean("reply_chars"),
        "mean_duration_when_solved_s": (
            round(sum(solved_times) / len(solved_times), 1) if solved_times else None
        ),
        "nonzero_exit": sum(1 for r in rows if r["telemetry"].get("exit_code") not in (0, None)),
    }


def _by_task_table(results: list[dict], paired: list[str]) -> list[dict]:
    """One row per task: runs, fixes and near-misses under each condition."""
    grouped: dict[str, dict[str, list[dict]]] = {}
    for row in results:
        arm = canonical_condition(row["condition"])
        grouped.setdefault(row["task_id"], {}).setdefault(arm, []).append(row)

    table = []
    for task_id in sorted(grouped):
        rows_all = [r for rows in grouped[task_id].values() for r in rows]
        entry = {
            "task_id": task_id,
            "repo": rows_all[0].get("repo", task_id),
            "error_type": rows_all[0].get("error_type") or [],
            "memory_count": rows_all[0].get("memory_count", 0),
            "paired": task_id in paired,
            "gold_files": rows_all[0]["gold_files"],
        }
        for condition in ALL_CONDITIONS:
            rows = grouped[task_id].get(condition, [])
            entry[condition] = {
                "runs": len(rows),
                "solved": sum(1 for r in rows if r["solved"]),
                "exact": sum(1 for r in rows if r["exact_match"]),
                "localized": sum(1 for r in rows if r["localized"]),
                "partial_credit": (
                    round(sum(r["partial_credit"] for r in rows) / len(rows), 4) if rows else None
                ),
                "mean_recall": (
                    round(sum(r["file_recall"] for r in rows) / len(rows), 4) if rows else None
                ),
                "mean_duration_s": _mean_duration(rows),
                "runs_detail": sorted(
                    (
                        {
                            "run": r["run"],
                            "solved": r["solved"],
                            # The executed outcome and whether it contradicts the judge.
                            # Both travel with the row: a page that shows a rate without
                            # showing which oracle produced it, and where the two
                            # disagree, is asking the reader to take the model's word.
                            "ci": r.get("ci"),
                            "judge_solved": r.get("judge_solved", r["solved"]),
                            "disagrees": bool(r.get("disagrees")),
                            "exact": r["exact_match"],
                            "files_matched": r["files_matched"],
                            "files_expected": r["files_expected"],
                            "file_recall": r["file_recall"],
                            "file_precision": r["file_precision"],
                            "file_iou": r["file_iou"],
                            "line_deviation_ratio": r["line_deviation_ratio"],
                            "predicted_files": r["predicted_files"],
                            "untouched_gold_files": r["untouched_gold_files"],
                            "collateral_files": r["collateral_files"],
                            "duration_s": r["telemetry"].get("duration_s"),
                            "reply_chars": r["telemetry"].get("reply_chars"),
                            "prompt_chars": r["telemetry"].get("prompt_chars"),
                        }
                        for r in rows
                    ),
                    key=lambda r: r["run"],
                ),
            }
        entry["total_runs"] = sum(entry[c]["runs"] for c in ALL_CONDITIONS)
        entry["total_solved"] = sum(entry[c]["solved"] for c in ALL_CONDITIONS)
        table.append(entry)
    return table


def _mean_duration(rows: list[dict]) -> float | None:
    values = [
        r["telemetry"]["duration_s"]
        for r in rows
        if r.get("telemetry", {}).get("duration_s") is not None
    ]
    return round(sum(values) / len(values), 1) if values else None


def _pass_at_k_curve(by_task: dict[tuple[str, str], list[dict]], paired: list[str]) -> dict:
    """Pass@K for every K the data supports.

    Pass@1 is single-shot accuracy; Pass@K at the largest K is what the agent could
    reach if allowed to retry. Memory may move one without moving the other, and a
    single K would hide that.
    """
    if not paired:
        return {"k": [], "series": {}, "tasks_at_k": []}
    depth = max(
        len(by_task.get((task, condition), []))
        for task in paired
        for condition in ALL_CONDITIONS
        if (task, condition) in by_task
    )

    # Mid-batch, only a handful of tasks have their full ten runs. Plotting K where the
    # sample has collapsed to two tasks draws a cliff that is an artefact of which tasks
    # finished first, not of the agent, so those K are withheld rather than shown.
    floor = max(3, round(0.25 * len(paired)))

    ks: list[int] = []
    tasks_at_k: list[int] = []
    withheld: list[int] = []
    series: dict[str, list[float]] = {c: [] for c in ALL_CONDITIONS}
    for k in range(1, depth + 1):
        # A task enters at this K only if both conditions have K runs. Averaging over
        # a different task set per condition would compare two different benchmarks.
        eligible = [
            task
            for task in paired
            if all(len(by_task.get((task, c), [])) >= k for c in PAIR)
        ]
        if len(eligible) < floor:
            withheld.append(k)
            continue
        ks.append(k)
        tasks_at_k.append(len(eligible))
        for condition in ALL_CONDITIONS:
            # Only tasks that actually have k runs in this arm. Pass@k over an arm a
            # task never had is not zero, it is undefined, and asking for it raises.
            values = [
                pass_at_k(
                    len(by_task[(task, condition)]),
                    sum(1 for r in by_task[(task, condition)] if r["solved"]),
                    k,
                )
                for task in eligible
                if len(by_task.get((task, condition), [])) >= k
            ]
            if not values:
                continue
            series[condition].append(round(sum(values) / len(values), 4))
    return {
        "k": ks,
        "series": series,
        "tasks_at_k": tasks_at_k,
        "tasks": len(paired),
        "withheld_k": withheld,
        "min_tasks": floor,
        "depth": depth,
    }


def _funnel(results: list[dict]) -> list[dict]:
    """Where runs drop out, from editing anything to matching the fix.

    Repair is localisation followed by editing. Splitting the two says whether a
    failure was "looked in the wrong place" or "right place, wrong change", which
    a single accuracy number cannot.
    """
    # The last three stages are three different bars, from loosest to strictest:
    # repaired the cause, wrote the maintainer's code, wrote their exact bytes. The gap
    # between the first and the second is the size of the measurement problem.
    stages = [
        ("Scored runs", lambda r: True),
        ("Edited some file", lambda r: bool(r["predicted_files"])),
        ("Touched a gold file", lambda r: r["touched_any_gold"]),
        ("Touched every gold file", lambda r: r["localized"]),
        ("Repaired the failure (judge)", lambda r: r["solved"]),
        ("Matched the real fix textually", lambda r: r["normalized_match"]),
        ("Byte-identical", lambda r: r["exact_match"]),
    ]
    table = []
    for name, test in stages:
        entry = {"stage": name}
        for condition in ALL_CONDITIONS:
            rows = [r for r in results if r["condition"] == condition]
            entry[condition] = sum(1 for r in rows if test(r))
            entry[f"{condition}_total"] = len(rows)
        table.append(entry)
    return table


def _histogram(results: list[dict], metric: str, bins: int = 5) -> dict:
    """Distribution of a per-run measure, so a mean is not read as a typical value."""
    edges = [i / bins for i in range(bins + 1)]
    labels = [f"{int(edges[i] * 100)}-{int(edges[i + 1] * 100)}%" for i in range(bins)]
    series: dict[str, list[int]] = {}
    for condition in ALL_CONDITIONS:
        counts = [0] * bins
        for row in results:
            if canonical_condition(row["condition"]) != condition:
                continue
            index = min(bins - 1, int(float(row[metric]) * bins))
            counts[index] += 1
        series[condition] = counts
    return {"labels": labels, "series": series, "metric": metric}


def _by_error_type(results: list[dict]) -> list[dict]:
    """Stratify by CI failure category.

    Project precedent should plausibly help on dependency and environment failures and
    not on syntax errors, so a pooled number can hide opposite effects.
    """
    buckets: dict[str, dict[str, list[dict]]] = {}
    for row in results:
        arm = canonical_condition(row["condition"])
        for error in row.get("error_type") or ["unknown"]:
            buckets.setdefault(error, {}).setdefault(arm, []).append(row)

    table = []
    for error, conditions in sorted(buckets.items()):
        entry = {"error_type": error}
        for condition in ALL_CONDITIONS:
            rows = conditions.get(condition, [])
            solved = sum(1 for r in rows if r["solved"])
            entry[condition] = {
                "runs": len(rows),
                "solved": solved,
                "rate": round(solved / len(rows), 4) if rows else None,
                "localized": sum(1 for r in rows if r["localized"]),
                "mean_recall": (
                    round(sum(r["file_recall"] for r in rows) / len(rows), 4) if rows else None
                ),
                "mean_duration_s": _mean_duration(rows),
            }
        entry["total_runs"] = sum(entry[c]["runs"] for c in ALL_CONDITIONS)
        table.append(entry)
    return sorted(table, key=lambda e: -e["total_runs"])


def export_rows(tasks_root: Path, runs_root: Path, agent: str) -> list[dict]:
    """Flat per-run table, one row per run, for statistics software or a paper."""
    data = score_task(tasks_root, runs_root, agent, None)
    rows = []
    for r in data["results"]:
        stamp = r.get("telemetry") or {}
        rows.append(
            {
                "agent": agent,
                "task_id": r["task_id"],
                "repo": r.get("repo", ""),
                "condition": r["condition"],
                "run": r["run"],
                "error_type": "|".join(r.get("error_type") or []),
                "memory_items": r.get("memory_count", 0),
                "solved": int(r["solved"]),
                # The judge's reasoning travels with the row, so a reviewer can check a
                # verdict without opening the run folder, and so the count of repairs
                # that took a different route than the maintainer is recoverable.
                "judged": int(bool(r.get("judged"))),
                "judge_cheats": int(bool((r.get("judge") or {}).get("cheats"))),
                "judge_mechanism": (r.get("judge") or {}).get("mechanism", ""),
                "judge_confidence": (r.get("judge") or {}).get("confidence", ""),
                "normalized_match": int(r["normalized_match"]),
                "exact_match": int(r["exact_match"]),
                "localized": int(r["localized"]),
                "touched_any_gold": int(r["touched_any_gold"]),
                "files_matched": r["files_matched"],
                "files_expected": r["files_expected"],
                "partial_credit": r["partial_credit"],
                "file_recall": r["file_recall"],
                "file_precision": r["file_precision"],
                "file_f1": r["file_f1"],
                "file_iou": r["file_iou"],
                "line_deviation_ratio": r["line_deviation_ratio"],
                "gold_line_delta": r["gold_line_delta"],
                "predicted_line_delta": r["predicted_line_delta"],
                "n_gold_files": len(r["gold_files"]),
                "n_predicted_files": len(r["predicted_files"]),
                "n_collateral_files": len(r["collateral_files"]),
                "duration_s": stamp.get("duration_s"),
                "prompt_chars": stamp.get("prompt_chars"),
                "reply_chars": stamp.get("reply_chars"),
                "exit_code": stamp.get("exit_code"),
                "finished_at": stamp.get("finished_at"),
            }
        )
    return sorted(rows, key=lambda r: (r["task_id"], r["condition"], r["run"]))


def run_detail(
    tasks_root: Path, runs_root: Path, agent: str, task_id: str, condition: str, run: str
) -> dict:
    """Everything about a single run: what it was asked, what it said, what it changed."""
    task = next((t for t in list_tasks(tasks_root) if t.task_id == task_id), None)
    if task is None:
        raise ValueError(f"unknown task {task_id!r}")
    run_dir = runs_root / agent / task_id / condition / run
    workspace = run_dir / "workspace"

    evaluation = None
    if workspace.exists() and is_edited(workspace, task.repo_before):
        evaluation = evaluate_submission(
            workspace, task.repo_before, task.repo_after, task_id, condition
        ).as_dict()
        evaluation.update(_verdict_fields(run_dir))

    def read(name: str) -> str:
        path = run_dir / name
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    return {
        "task_id": task_id,
        "repo": task.repo_name or task_id,
        "condition": condition,
        "run": run,
        "error_type": task.error_type,
        "memory_count": len(task.memory),
        "prompt": read("prompt.md") or build_prompt(task, condition),
        "response": read("agent_response.md"),
        "stderr": read("agent_stderr.txt")[:4000],
        "telemetry": telemetry(run_dir),
        "evaluation": evaluation,
        "agent_diff": _diff(task.repo_before, workspace),
        "gold_diff": _diff(task.repo_before, task.repo_after),
    }


def _diff(before: Path, after: Path, max_files: int = 12, max_lines: int = 400) -> str:
    """Unified diff between two snapshots, trimmed to stay readable in a browser."""
    import difflib

    if not after.exists():
        return ""
    chunks: list[str] = []
    old_files, new_files = _files(before), _files(after)
    names = sorted(set(old_files) | set(new_files))
    shown = 0
    for name in names:
        old = old_files.get(name)
        new = new_files.get(name)
        if old == new:
            continue
        if shown >= max_files:
            chunks.append(f"... {len(names) - shown} further file(s) not shown")
            break
        shown += 1
        lines = list(
            difflib.unified_diff(
                (old or "").splitlines(),
                (new or "").splitlines(),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
                lineterm="",
                n=3,
            )
        )
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more diff lines"]
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


def export_csv(tasks_root: Path, runs_root: Path, agent: str) -> str:
    import csv
    import io

    rows = export_rows(tasks_root, runs_root, agent)
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator=chr(10))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def export_latex(tasks_root: Path, runs_root: Path, agent: str) -> str:
    """A per-task results table plus the paired test, ready to paste into a paper."""
    data = score_task(tasks_root, runs_root, agent, None)
    analysis = data["summary"]["analysis"]

    per_task: dict[str, dict[str, list[dict]]] = {}
    for row in data["results"]:
        arm = canonical_condition(row["condition"])
        per_task.setdefault(row["task_id"], {}).setdefault(arm, []).append(row)

    solved_no = analysis["both_solved"] + analysis["only_no_memory"]
    solved_with = analysis["both_solved"] + analysis["only_with_memory"]

    lines = [
        "% Generated by ci-memory-agents",
        r"\begin{table}[t]",
        r"\centering",
        rf"\caption{{CI repair success with and without repository memory ({agent}).}}",
        r"\label{tab:memory-effect}",
        r"\begin{tabular}{lrrl}",
        r"\toprule",
        r"Task & Without memory & With memory & Outcome \\",
        r"\midrule",
    ]

    for task in sorted(per_task):
        conditions = per_task[task]
        # The two arms of the RQ1 contrast, in that order -- this table has exactly two
        # columns. Iterating every arm on disk and then taking the first two by position
        # would fill them with no_memory and memory_k1 the moment the K sweep was added,
        # under a header that still said "With memory".
        cells, rates = [], {}
        for condition in PAIR:
            rows = conditions.get(condition, [])
            if rows:
                solved = sum(1 for r in rows if r["solved"])
                cells.append(f"{solved}/{len(rows)}")
                rates[condition] = solved / len(rows)
            else:
                cells.append("--")
                rates[condition] = None
        before, after = rates[PAIR[0]], rates[PAIR[1]]
        if before is None or after is None:
            outcome = "incomplete"
        elif before == after:
            outcome = "tie"
        elif after > before:
            outcome = "memory better"
        else:
            outcome = "memory worse"
        lines.append(f"{task.replace('_', chr(92) + '_')} & {cells[0]} & {cells[1]} & {outcome} " + r"\\")

    lines += [
        r"\midrule",
        rf"Tasks solved & {solved_no}/{analysis['n_tasks']} & "
        rf"{solved_with}/{analysis['n_tasks']} & " + r"\\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        "",
        f"% Exact McNemar on {analysis['discordant']} discordant pairs: p = {analysis['p_value']}",
        f"% 95% CI without memory: {analysis['ci_no_memory']}",
        f"% 95% CI with memory:    {analysis['ci_with_memory']}",
        f"% {analysis['interpretation']}",
    ]
    return "\n".join(lines)


def open_in_editor(path: Path) -> str:
    """Open a workspace in VS Code, falling back to the OS file browser."""
    if not path.exists():
        return f"missing: {path}"
    for command in (["code", str(path)], ["explorer", str(path)]):
        try:
            subprocess.Popen(command, shell=(command[0] == "code" and sys.platform == "win32"))
            return f"opened with {command[0]}"
        except (FileNotFoundError, OSError):
            continue
    return "could not open"


def reset_run(workspace: Path, repo_before: Path) -> str:
    import shutil

    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    shutil.copytree(repo_before, workspace)
    invalidate_scores()
    return "reset"


def list_agents(runs_root: Path) -> list[str]:
    """Agent folders under runs/, so the UI can switch between them.

    An agent folder holds task folders holding condition folders; the execution oracle's
    clone cache (`_repos`) sits in the same directory and is not one. Matched on shape
    rather than by name, so any future sibling is excluded for the same reason.
    """
    if not runs_root.exists():
        return []
    agents = []
    for path in sorted(runs_root.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        if any(task.is_dir() and any(c.is_dir() for c in task.iterdir()) for task in path.iterdir()):
            agents.append(path.name)
    return agents
