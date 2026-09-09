"""Write every result the three research questions need, as CSV, for all agents at once.

    python scripts/export_results.py
    python scripts/export_results.py --agent copilot-gpt-5.4-mini

The point of this script is that analysis should never require re-running agents. Runs
cost model credits; reading a CSV costs nothing, and the numbers in a paper should be
recomputable from files in the repository months later, on a machine with no API key.
Everything below is written on every invocation, overwriting the previous copy, so the
directory is always one consistent snapshot rather than a pile of partial exports.

    runs.csv               one row per run: the raw long-format table everything else
                           is derived from. Keep this one if you keep nothing else.
    rq1_memory_effect.csv  RQ1: memory vs no memory, paired per task, per agent.
    rq2_consistency.csv    RQ2: the spread across repeated runs of the same cell.
    rq3_memory_size.csv    RQ3: success as a function of K.
    by_error_group.csv     RQ1 split across the three problem groups.
    by_repo.csv            RQ1 split across the ten repositories.
    population.csv         which tasks were studied, and what they are.

Source of truth is `runs/verdicts_<agent>.jsonl`, the oracle-resolved verdict per run
written by `scripts/score_runs.py --mode report`. That file records *how* each run was
decided -- executed workflow, deterministic check, or model judgement -- and the CSVs
carry that column through. A rate whose provenance is mostly "judge" is a weaker claim
than the same rate from execution, and dropping the column would let the two be quoted
as though they were the same number.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ci_memory_agents import design
from ci_memory_agents.evaluator import pass_at_k
from ci_memory_agents.loader import list_tasks
from ci_memory_agents.prompt_builder import (
    ALL_CONDITIONS,
    CONDITIONS,
    NO_MEMORY,
    PAIR,
    arm_kind,
    canonical_condition,
    condition_k,
)
from ci_memory_agents.stats import paired_analysis, wilson_interval


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in columns})
    return path


def _cell(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return "|".join(str(item) for item in value)
    if isinstance(value, float):
        return round(value, 6)
    return "" if value is None else value


def discover_agents(runs_root: Path) -> list[str]:
    """Agent directories that actually hold runs.

    Taken from disk rather than from `design.AGENTS` so that exploratory agents and
    agents from earlier rounds still export. An agent in the design that has not been
    run yet simply produces no rows, which is the honest representation of "not run".
    """
    if not runs_root.exists():
        return []
    return sorted(
        path.name
        for path in runs_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )


def telemetry(run_dir: Path) -> dict:
    path = run_dir / "agent_meta.json"
    if not path.exists():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    duration = meta.get("duration_ms")
    return {
        "duration_s": round(duration / 1000, 1) if isinstance(duration, (int, float)) else None,
        "prompt_chars": meta.get("prompt_chars"),
        "reply_chars": meta.get("reply_chars"),
        "exit_code": meta.get("exit_code"),
        # Recorded by the runner from its own configuration. It is the only place the
        # model behind a run is written down at the time the run happened, which is why
        # it is preferred over the design registry when both are available: the registry
        # says what an agent name means *today*.
        "model": meta.get("model") or "",
        "finished_at": meta.get("finished_at"),
    }


RUN_COLUMNS = [
    "agent", "harness", "model", "task_id", "repo", "error_group", "error_type",
    "condition", "k", "run", "attempted", "decided", "solved", "oracle",
    "ci_conclusion", "static_verdict", "judge_solved", "unstable", "cheats",
    "confidence", "reason", "duration_s", "prompt_chars", "reply_chars", "exit_code",
    "finished_at", "arm_kind", "analysis_condition",
]


def collect_runs(runs_root: Path, tasks_root: Path, agents: list[str]) -> list[dict]:
    tasks = {task.task_id: task for task in list_tasks(tasks_root)}
    rows: list[dict] = []
    for agent in agents:
        verdict_path = runs_root / f"verdicts_{agent}.jsonl"
        if not verdict_path.exists():
            print(
                f"  {agent}: no verdicts_{agent}.jsonl -- run "
                f"`python scripts/score_runs.py --agent {agent} --mode report` first. Skipped."
            )
            continue
        config = design.agent_config(agent)
        verdicts = [
            json.loads(line)
            for line in verdict_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for verdict in verdicts:
            task = tasks.get(verdict["task_id"])
            condition = verdict["condition"]
            run_dir = runs_root / agent / verdict["task_id"] / condition / verdict["run"]
            stamp = telemetry(run_dir)
            rows.append(
                {
                    **verdict,
                    "agent": agent,
                    "harness": config["harness"],
                    "model": stamp.get("model") or config["model"],
                    "repo": task.repo_name if task else "",
                    "error_group": task.error_group if task else "",
                    "k": condition_k(condition),
                    # `design`, `legacy` or `control`. Runs outside the design are
                    # real data and are exported, but the column lets an analysis of
                    # the K curve exclude them: a legacy arm's K is inferred from an
                    # old name, and the placebo arm's items were never this project's
                    # history at all.
                    "arm_kind": arm_kind(condition),
                    # The arm this run is *analysed* as. `condition` stays exactly as
                    # it is on disk; this column folds the renamed arm onto the design
                    # arm it is the same treatment as, so one condition's evidence is
                    # not split across two names. RQ1 and RQ3 group by this.
                    "analysis_condition": canonical_condition(condition),
                    "duration_s": stamp.get("duration_s"),
                    "prompt_chars": stamp.get("prompt_chars"),
                    "reply_chars": stamp.get("reply_chars"),
                    "exit_code": stamp.get("exit_code"),
                    "finished_at": stamp.get("finished_at"),
                }
            )
        print(f"  {agent}: {len(verdicts)} runs")
    return rows


def scored(rows: list[dict]) -> list[dict]:
    """Runs an oracle actually decided.

    Unattempted cells are excluded everywhere. The harness materialises a prompt and a
    pristine workspace for every planned run, so counting them would score missing data
    as failed repairs -- the defect that once moved this project's reported rate from
    57% to 17%.
    """
    return [r for r in rows if r.get("attempted", True) and r.get("decided")]


def rate_row(rows: list[dict], **labels) -> dict:
    solved = sum(1 for r in rows if r["solved"])
    total = len(rows)
    interval = wilson_interval(solved, total) if total else None
    return {
        **labels,
        "runs": total,
        "solved": solved,
        "rate": round(solved / total, 4) if total else None,
        "ci_low": round(interval.low, 4) if interval else None,
        "ci_high": round(interval.high, 4) if interval else None,
    }


def task_success(rows: list[dict]) -> dict[tuple[str, str, str], bool]:
    """Per (agent, task, condition): solved in at least one run.

    Binarising this way is what makes the paired McNemar test applicable, and it is the
    same definition the dashboard and the LaTeX table use.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["agent"], row["task_id"], row["analysis_condition"])].append(row)
    return {key: any(r["solved"] for r in group) for key, group in grouped.items()}


def rq1(rows: list[dict]) -> list[dict]:
    control, memory = PAIR
    success = task_success(rows)
    out = []
    for agent in sorted({r["agent"] for r in rows}):
        per_task: dict[str, dict[str, bool]] = defaultdict(dict)
        for (row_agent, task_id, condition), solved in success.items():
            if row_agent == agent and condition in PAIR:
                per_task[task_id][condition] = solved
        analysis = paired_analysis(per_task, pair=PAIR)
        out.append(
            {
                "agent": agent,
                "control_arm": control,
                "memory_arm": memory,
                "n_paired_tasks": analysis["n_tasks"],
                "solved_control": analysis["both_solved"] + analysis["only_no_memory"],
                "solved_memory": analysis["both_solved"] + analysis["only_with_memory"],
                "rate_control": analysis["rate_no_memory"],
                "rate_memory": analysis["rate_with_memory"],
                "difference": analysis["difference"],
                "both_solved": analysis["both_solved"],
                "neither_solved": analysis["neither_solved"],
                "only_control": analysis["only_no_memory"],
                "only_memory": analysis["only_with_memory"],
                "discordant": analysis["discordant"],
                "p_value": analysis["p_value"],
                "significant": analysis["significant"],
                "interpretation": analysis["interpretation"],
            }
        )
    return out


def rq2(rows: list[dict]) -> list[dict]:
    """Consistency across the repeats of one cell.

    `solved_runs` out of `runs` is the cell's success rate; `all_or_nothing` says the
    agent was unanimous. An agent that solves a task on 3 of 5 runs and one that solves
    it on 5 of 5 both count as "solved" in RQ1's binarisation, and this table is where
    that difference survives.
    """
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[
            (row["agent"], row["model"], row["task_id"], row["repo"], row["analysis_condition"])
        ].append(row)

    out = []
    for (agent, model, task_id, repo, condition), group in sorted(grouped.items()):
        solved = sum(1 for r in group if r["solved"])
        total = len(group)
        rate = solved / total
        durations = [r["duration_s"] for r in group if r.get("duration_s") is not None]
        out.append(
            {
                "agent": agent,
                "model": model,
                "task_id": task_id,
                "repo": repo,
                "condition": condition,
                "k": condition_k(condition),
                "runs": total,
                "solved_runs": solved,
                "rate": round(rate, 4),
                "all_or_nothing": solved in (0, total),
                # Which on-disk arms these repeats came from. Normally one; two when a
                # renamed arm was folded in, which is worth seeing because those repeats
                # were collected in a different session.
                "source_arms": sorted({r["condition"] for r in group}),
                # Bernoulli sd of the per-run outcome. 0 when every repeat agreed, at
                # its maximum of 0.5 when the agent solved exactly half of them.
                "sd": round((rate * (1 - rate)) ** 0.5, 4),
                "unstable_runs": sum(1 for r in group if r.get("unstable")),
                "mean_duration_s": round(statistics.mean(durations), 1) if durations else None,
            }
        )
    return out


def rq3(rows: list[dict]) -> list[dict]:
    """Success against K, with the K=0 control as the reference point.

    Legacy arms are excluded. Their K is inferred from what `with_memory` used to mean
    rather than recorded when the run was made, and a dose-response curve must not rest
    on an inferred dose.
    """
    usable = [r for r in rows if r["analysis_condition"] in CONDITIONS]
    out = []
    for agent in sorted({r["agent"] for r in usable}):
        agent_rows = [r for r in usable if r["agent"] == agent]
        baseline = [r for r in agent_rows if r["analysis_condition"] == NO_MEMORY]
        baseline_rate = (
            sum(1 for r in baseline if r["solved"]) / len(baseline) if baseline else None
        )
        for condition in sorted(
            {r["analysis_condition"] for r in agent_rows}, key=lambda c: condition_k(c)
        ):
            group = [r for r in agent_rows if r["analysis_condition"] == condition]
            row = rate_row(
                group,
                agent=agent,
                model=group[0]["model"],
                condition=condition,
                k=condition_k(condition),
            )
            row["difference_vs_k0"] = (
                round(row["rate"] - baseline_rate, 4)
                if row["rate"] is not None and baseline_rate is not None
                else None
            )

            # Pass@1 over tasks, alongside the per-run rate. The per-run rate weights a
            # task by how many runs it has; Pass@1 weights every task equally, which is
            # the number CI-Repair-Bench reports and the one comparable to their band.
            per_task: dict[str, list[dict]] = defaultdict(list)
            for entry in group:
                per_task[entry["task_id"]].append(entry)
            scores = [
                pass_at_k(len(runs), sum(1 for r in runs if r["solved"]), 1)
                for runs in per_task.values()
            ]
            row["n_tasks"] = len(per_task)
            row["pass_at_1"] = round(statistics.mean(scores), 4) if scores else None
            out.append(row)
    return out


def split_by(rows: list[dict], key: str) -> list[dict]:
    """RQ1's contrast computed inside each level of `key` (problem group, repository)."""
    out = []
    for agent in sorted({r["agent"] for r in rows}):
        for level in sorted({r[key] or "unknown" for r in rows if r["agent"] == agent}):
            for condition in sorted(
                {r["analysis_condition"] for r in rows if r["agent"] == agent},
                key=condition_k,
            ):
                group = [
                    r
                    for r in rows
                    if r["agent"] == agent
                    and (r[key] or "unknown") == level
                    and r["analysis_condition"] == condition
                ]
                if not group:
                    continue
                out.append(
                    rate_row(
                        group,
                        agent=agent,
                        **{key: level},
                        condition=condition,
                        k=condition_k(condition),
                        n_tasks=len({r["task_id"] for r in group}),
                    )
                )
    return out


def population(tasks_root: Path) -> list[dict]:
    return [
        {
            "task_id": task.task_id,
            "repo": task.repo_name,
            "error_group": task.error_group,
            "error_type": task.error_type,
            "source": task.source,
            "commit_date": task.commit_date,
            "memory_available": task.memory_size,
            "supports_max_k": task.supports(max(design.K_VALUES)),
            "gold_files": task.target_files,
        }
        for task in list_tasks(tasks_root)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--out", default=str(REPO_ROOT / "results"))
    parser.add_argument(
        "--agent",
        action="append",
        default=None,
        help="Limit to this agent. Repeatable. Default: every agent with runs on disk.",
    )
    parser.add_argument(
        "--strict-arms",
        action="store_true",
        help=(
            "Analyse only runs collected under the current arm names, dropping the "
            "renamed `with_memory` arm instead of folding it onto memory_k3. Use when "
            "a reviewer objects to treating the two as one condition; expect a much "
            "smaller sample."
        ),
    )
    args = parser.parse_args()

    runs_root = Path(args.runs_root)
    tasks_root = Path(args.tasks_root)
    out_root = Path(args.out)

    agents = args.agent or discover_agents(runs_root)
    if not agents:
        print(f"No agents with runs under {runs_root}.")
        return 1

    print(f"Reading verdicts for {len(agents)} agent(s):")
    rows = collect_runs(runs_root, tasks_root, agents)
    if not rows:
        print("\nNothing to export. Score some runs first:")
        print("  python scripts/score_runs.py --agent <agent> --mode report")
        return 1

    decided = scored(rows)
    if args.strict_arms:
        dropped = [r for r in decided if r["arm_kind"] == "legacy"]
        decided = [r for r in decided if r["arm_kind"] != "legacy"]
        print(f"--strict-arms: dropped {len(dropped)} runs collected under a renamed arm.")
    print(
        f"\n{len(rows)} runs on disk, {len(decided)} decided by an oracle. "
        "Rates below and in every CSV except runs.csv are over the decided runs; "
        "runs.csv keeps all of them, with `attempted` and `decided` as columns."
    )

    folded = sum(1 for r in decided if r["arm_kind"] == "legacy")
    if folded:
        print(
            f"{folded} of them were collected as `with_memory` and are analysed as "
            f"memory_k3, the same treatment under its current name. runs.csv keeps both "
            f"the on-disk `condition` and the `analysis_condition` used here; pass "
            f"--strict-arms to exclude them instead."
        )

    written = [
        write_csv(out_root / "runs.csv", RUN_COLUMNS, rows),
        write_csv(
            out_root / "rq1_memory_effect.csv",
            [
                "agent", "control_arm", "memory_arm", "n_paired_tasks", "solved_control",
                "solved_memory", "rate_control", "rate_memory", "difference",
                "both_solved", "neither_solved", "only_control", "only_memory",
                "discordant", "p_value", "significant", "interpretation",
            ],
            rq1(decided),
        ),
        write_csv(
            out_root / "rq2_consistency.csv",
            [
                "agent", "model", "task_id", "repo", "condition", "k", "runs",
                "solved_runs", "rate", "all_or_nothing", "sd", "unstable_runs",
                "mean_duration_s", "source_arms",
            ],
            rq2(decided),
        ),
        write_csv(
            out_root / "rq3_memory_size.csv",
            [
                "agent", "model", "condition", "k", "runs", "solved", "rate",
                "ci_low", "ci_high", "difference_vs_k0", "n_tasks", "pass_at_1",
            ],
            rq3(decided),
        ),
        write_csv(
            out_root / "by_error_group.csv",
            ["agent", "error_group", "condition", "k", "n_tasks", "runs", "solved",
             "rate", "ci_low", "ci_high"],
            split_by(decided, "error_group"),
        ),
        write_csv(
            out_root / "by_repo.csv",
            ["agent", "repo", "condition", "k", "n_tasks", "runs", "solved", "rate",
             "ci_low", "ci_high"],
            split_by(decided, "repo"),
        ),
        write_csv(
            out_root / "population.csv",
            ["task_id", "repo", "error_group", "error_type", "source", "commit_date",
             "memory_available", "supports_max_k", "gold_files"],
            population(tasks_root),
        ),
    ]

    print(f"\nWrote {len(written)} files to {out_root}:")
    for path in written:
        line_count = sum(1 for _ in path.open(encoding="utf-8")) - 1
        print(f"  {path.name:<26} {line_count:>5} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
