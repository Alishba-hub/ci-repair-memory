"""Score every run with the strongest oracle that can decide it, and report honestly.

    python scripts/score_runs.py --agent claude-code --mode execute --parallel 4
    python scripts/score_runs.py --agent claude-code --mode execute --backend local
    python scripts/score_runs.py --agent claude-code --mode report
    python scripts/score_runs.py --agent claude-code --mode agreement

`--mode execute` re-runs the failing workflow with each candidate patch applied and
caches the outcome next to the run. That is CI-Repair-Bench's oracle and it is what the
headline number should rest on.

`--mode report` combines everything on disk -- deterministic checks, CI outcomes,
judgements -- into one verdict per run via `oracle.resolve`, then reports the rate under
each condition together with the breakdown of which oracle decided what. A rate whose
provenance column is mostly "judge" is a weaker claim than the same rate whose column is
mostly "execution", and the table says which one this is instead of leaving a reader to
assume.

`--mode agreement` is the table that answers the objection to LLM judging directly:
Cohen's kappa between the judge and full CI re-execution, on the runs where both ran.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ci_memory_agents.agreement import by_condition, judge_vs_execution
from ci_memory_agents.ci_outcome import outcome_path, read_outcome, write_outcome
from ci_memory_agents.evaluator import pass_at_k
from ci_memory_agents.loader import list_tasks
from ci_memory_agents.oracle import (
    candidate_diff,
    instance_is_usable,
    resolve,
    run_attempted,
    write_verdicts,
)
from ci_memory_agents.prompt_builder import (
    ALL_CONDITIONS,
    CONDITIONS,
    PAIR,
    canonical_condition,
    condition_k,
)
from ci_memory_agents.tokens import (
    Usage,
    estimate_tokens,
    fmt_cost,
    fmt_tokens,
    read as read_usage,
)
from ci_memory_agents.stats import (
    paired_rate_analysis,
    per_task_sd,
    tasks_for_power,
)


def conditions_on(task_dir: Path) -> list[str]:
    """The experimental arms this task actually has, in experiment order.

    Anything on disk that is a directory is an arm. Known ones come first so tables read
    in a fixed order; an unrecognised one still appears rather than vanishing.
    """
    if not task_dir.is_dir():
        return []
    found = [p.name for p in task_dir.iterdir() if p.is_dir()]
    known = [c for c in ALL_CONDITIONS if c in found]
    return known + sorted(c for c in found if c not in ALL_CONDITIONS)


def collect(runs_root: Path, tasks_root: Path, agent: str, args) -> list[tuple[Path, object, str]]:
    tasks = {task.task_id: task for task in list_tasks(tasks_root)}
    agent_root = runs_root / agent
    if not agent_root.exists():
        raise SystemExit(f"No runs folder at {agent_root}")
    found = []
    for task_dir in sorted(agent_root.iterdir()):
        if args.task_id and task_dir.name != args.task_id:
            continue
        task = tasks.get(task_dir.name)
        if task is None:
            continue
        if args.usable_only:
            if not instance_is_usable(task.root)[0]:
                continue
        # Read the arms off disk rather than hardcoding them. A hardcoded list once
        # meant an arm's completed runs were silently dropped by the scorer -- invisible
        # in the very table whose job is to say what is missing.
        for condition in conditions_on(task_dir):
            if args.condition and condition != args.condition:
                continue
            condition_dir = task_dir / condition
            for run_dir in sorted(condition_dir.iterdir()):
                if run_dir.is_dir() and (run_dir / "workspace").exists():
                    found.append((run_dir, task, condition))
    return found


def command_execute(args, runs_root: Path, tasks_root: Path) -> int:
    runs = collect(runs_root, tasks_root, args.agent, args)

    def needs_running(run_dir: Path) -> bool:
        if args.force:
            return True
        cached = read_outcome(outcome_path(run_dir))
        # An inconclusive outcome is worth retrying: it usually means the workflow never
        # got a chance to speak, not that the patch was wrong.
        return cached is None or not cached.decided

    # Never execute a cell no agent has run. Its workspace is a pristine copy, so CI
    # would faithfully report the original failure -- a real Actions build, several
    # minutes, spent confirming that an empty patch does not fix anything. At six
    # minutes each, the 322 unrun cells in this repository would be about a day.
    runs = [item for item in runs if run_attempted(item[0])]
    pending = [item for item in runs if needs_running(item[0])]
    if args.limit:
        pending = pending[: args.limit]

    print(f"Runs an agent actually ran : {len(runs)}")
    print(f"To execute                : {len(pending)}  (cached: {len(runs) - len(pending)})")
    if args.dry_run:
        for run_dir, _, _ in pending[:20]:
            print("  would run", "/".join(run_dir.parts[-3:]))
        return 0
    if not pending:
        print("\nNothing to execute. Report with --mode report.")
        return 0

    if args.backend == "github":
        from ci_memory_agents.oracle_github import GitHubConfig, run_ci

        config = GitHubConfig.from_env(Path(args.repos_dir), owner=args.owner)
        config.poll_interval = args.poll_interval
        config.poll_timeout = args.poll_timeout

        def execute(item):
            run_dir, task, condition = item
            diff = candidate_diff(run_dir, task)
            label = f"{condition}-{run_dir.name}"
            outcome = run_ci(config, task, diff, label)
            outcome.source = "github"
            write_outcome(outcome_path(run_dir), outcome)
            return run_dir, outcome
    else:
        from ci_memory_agents.oracle_local import LocalConfig, run_ci as run_local

        config = LocalConfig(
            backend=args.backend.split(":", 1)[-1] if ":" in args.backend else "docker",
            timeout=args.poll_timeout,
            workdir=Path(args.repos_dir),
        )

        def execute(item):
            run_dir, task, _ = item
            metadata = json.loads((task.root / "metadata.json").read_text(encoding="utf-8"))
            diff = candidate_diff(run_dir, task)
            outcome = run_local(task, metadata, diff, config)
            write_outcome(outcome_path(run_dir), outcome)
            return run_dir, outcome

    print(f"\nBackend: {args.backend}, {args.parallel} at a time\n")
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for index, (run_dir, outcome) in enumerate(pool.map(execute, pending), start=1):
            label = "/".join(run_dir.parts[-3:])
            print(f"[{index}/{len(pending)}] {outcome.conclusion:<13} {label}  {outcome.detail[:70]}")
    print("\nDone. Report with --mode report.")
    return 0


def _verdicts(args, runs_root: Path, tasks_root: Path):
    return [
        resolve(run_dir, task, condition, strict_unresolved=args.strict)
        for run_dir, task, condition in collect(runs_root, tasks_root, args.agent, args)
    ]


def _report_model_consistency(rows: list[dict], runs_root: Path, args) -> None:
    """Which model actually produced each run, and whether it stayed the same.

    A "family" can resolve to a router that picks a different underlying model per
    request. That makes the model an uncontrolled variable *inside* a condition, so a
    difference between arms may be the router rather than the treatment. The runner
    records the resolved id on every run; this reads them back and says whether the
    experiment was in fact run against one model.

    Split by condition as well as pooled, because that is where it does damage. One model
    throughout is fine even if it came from a router. A different mix in each arm is the
    effect being manufactured.
    """
    ids: Counter = Counter()
    per_condition: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        path = runs_root / args.agent / row["task_id"] / row["condition"] / row["run"] / "agent_meta.json"
        if not path.exists():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        model = meta.get("model_id") or meta.get("model_family")
        if not model:
            continue
        ids[model] += 1
        per_condition[row["condition"]][model] += 1

    if not ids:
        print()
        print("Model provenance")
        print("  !! no run records which model produced it. Runs from different models are")
        print("     indistinguishable after the fact; record it at run time.")
        return

    print()
    print("Model provenance")
    if len(ids) == 1:
        only, count = next(iter(ids.items()))
        print(f"  all {count} runs produced by {only}")
        return

    print(f"  !! runs come from MORE THAN ONE model: "
          f"{', '.join(f'{m} x{n}' for m, n in ids.most_common())}")
    for condition in sorted(per_condition):
        detail = ", ".join(f"{m} x{n}" for m, n in per_condition[condition].most_common())
        print(f"       {condition:<16}{detail}")
    print("     If the mix differs between conditions, the comparison is measuring the")
    print("     model as well as the treatment. Re-run against one pinned model before")
    print("     quoting any difference.")


def _report_judge_consistency(rows: list[dict], runs_root: Path, args) -> None:
    """Whether every judged run was decided by the same instrument, and how stable it was.

    Two different failure modes, both invisible in a rate.

    A *mixed* set is the worse one. Judge versions differ in what they do -- v3 added a
    deterministic pre-screen and three-sample majority voting -- so a set judged partly
    by one and partly by another is two measurements averaged together, and re-judging
    a few runs can move the headline without any run having changed. If the versions
    split unevenly across conditions it can move the *difference*, which is the whole
    result. Finish the re-judge before quoting anything.

    An *unstable* run is the milder one: the samples within a single judging disagreed,
    so that verdict would have come out differently on another draw. Countable, and
    worth stating next to any judge-derived number.
    """
    versions: Counter = Counter()
    per_condition: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        path = runs_root / args.agent / row["task_id"] / row["condition"] / row["run"] / "judgement.json"
        if not path.exists():
            continue
        try:
            version = json.loads(path.read_text(encoding="utf-8")).get("judge_version")
        except (json.JSONDecodeError, OSError):
            version = "unreadable"
        versions[version] += 1
        per_condition[row["condition"]][version] += 1

    unstable = sum(1 for r in rows if r.get("unstable"))
    if not versions and not unstable:
        return

    print("\nJudge consistency")
    if len(versions) > 1:
        spread = ", ".join(f"v{v}: {n}" for v, n in sorted(versions.items(), key=lambda kv: str(kv[0])))
        print(f"  !! verdicts come from MORE THAN ONE judge version ({spread}).")
        for condition in sorted(per_condition):
            detail = ", ".join(
                f"v{v}: {n}" for v, n in sorted(per_condition[condition].items(), key=lambda kv: str(kv[0]))
            )
            print(f"       {condition:<14}{detail}")
        print("     These are different instruments. A rate over a mixture is two")
        print("     measurements averaged, and an uneven split across conditions moves the")
        print("     difference itself. Re-judge everything with --force before quoting.")
    elif versions:
        only = next(iter(versions))
        print(f"  all {sum(versions.values())} verdicts from judge v{only}")

    if unstable:
        print(f"  {unstable} of {len(rows)} runs had samples disagree within one judging;")
        print("     each would have come out differently on another draw.")


def command_tokens(args, runs_root: Path, tasks_root: Path) -> int:
    """What the experiment has cost, and what the reductions saved.

    Reported per condition as well as in total, because the `with_memory` prompt is
    2.25x the `no_memory` one: memory is not free, and a benefit claimed without its
    cost beside it invites the reader to assume it was.
    """
    runs = collect(runs_root, tasks_root, args.agent, args)
    attempted = [(d, t, c) for d, t, c in runs if (d / "agent_meta.json").exists()]
    if not attempted:
        print("No attempted runs to account for.")
        return 1

    totals: dict[str, dict[str, Usage]] = defaultdict(lambda: defaultdict(Usage))
    for run_dir, _, condition in attempted:
        for kind, usage in read_usage(run_dir).items():
            totals[condition][kind] = totals[condition][kind].add(usage)
        # The agent's own call is not metered by the runner, so it is estimated from the
        # prompt and reply sizes the runner does record. Labelled as an estimate.
        try:
            meta = json.loads((run_dir / "agent_meta.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "agent" not in read_usage(run_dir):
            totals[condition]["agent"] = totals[condition]["agent"].add(
                Usage(
                    calls=1,
                    input_tokens=estimate_tokens("x" * int(meta.get("prompt_chars", 0))),
                    output_tokens=estimate_tokens("x" * int(meta.get("reply_chars", 0))),
                    source="estimated",
                    model=args.agent_model,
                )
            )

    print(f"Token accounting for {args.agent}   ({len(attempted)} attempted runs)")
    print()
    print(f"  {'condition':<14}{'what':<9}{'calls':>7}{'input':>10}{'output':>9}{'cost':>10}  source")
    grand = Usage()
    for condition in sorted(totals):
        for kind in ("agent", "judge"):
            usage = totals[condition].get(kind)
            if not usage or not (usage.calls or usage.calls_saved):
                continue
            print(
                f"  {condition:<14}{kind:<9}{usage.calls:>7}"
                f"{fmt_tokens(usage.input_tokens):>10}{fmt_tokens(usage.output_tokens):>9}"
                f"{fmt_cost(usage.cost):>10}  {usage.source}"
            )
            grand = grand.add(usage)
    print(f"  {'TOTAL':<23}{grand.calls:>7}{fmt_tokens(grand.input_tokens):>10}"
          f"{fmt_tokens(grand.output_tokens):>9}{fmt_cost(grand.cost):>10}  {grand.source}")

    if grand.source in ("estimated", "mixed"):
        print()
        print("  Figures marked `estimated` are derived from character counts, not")
        print("  reported by the CLI. Treat them as planning numbers, not measurements.")

    saved_calls = sum(u.calls_saved for c in totals.values() for u in c.values())
    saved_tokens = sum(u.tokens_saved for c in totals.values() for u in c.values())
    if saved_calls:
        would_be = grand.calls + saved_calls
        print()
        print(f"  Reductions saved {saved_calls} of {would_be} model calls "
              f"({saved_calls / would_be:.0%}), about {fmt_tokens(saved_tokens)} input tokens.")
        print("  Identical patches share one verdict; sampling stops once the majority is")
        print("  settled. Neither changes any verdict -- both skip a question already answered.")

    # What the scale this study's own power analysis calls for would cost.
    #
    # Projected per *run*, not per task. The tasks measured so far are unevenly filled --
    # five hold most of the runs and sixteen have one per arm -- so a per-task average
    # over them describes the current lopsided sample rather than a full task, and would
    # understate a complete design roughly threefold.
    if grand.cost and grand.calls:
        per_run = grand.cost / grand.calls
        print()
        print(f"  Cost per agent run: {fmt_cost(per_run)}  (mean over {grand.calls} runs)")
        print()
        print(f"  {'design':<38}{'runs':>8}{'projected':>12}")
        for tasks, arms, episodes, label in (
            (24, 2, 10, "the current plan, finished"),
            (363, 2, 5, "5 pp at 80% power, 5 episodes"),
        ):
            runs = tasks * arms * episodes
            print(f"  {label:<38}{runs:>8}{fmt_cost(per_run * runs):>12}")
        print()
        print("  Agent runs only. Judging adds roughly 3 calls per run at a fraction of")
        print("  the prompt size, and the execution oracle costs Actions minutes, not tokens.")
        print("  Row 2 is the one to read before committing to the full study.")
    return 0


def _repo_scope(task_root: Path) -> str:
    """"full" for a real checkout, "focused" for the gold-patch-files-only tree."""
    try:
        return json.loads((task_root / "metadata.json").read_text(encoding="utf-8")).get(
            "repo_scope", "focused"
        )
    except (json.JSONDecodeError, OSError):
        return "focused"


def _report_scope_split(rows: list[dict], tasks_root: Path, args) -> None:
    """Rates split by whether the agent held a repository or only the gold patch's files.

    These are different tasks and their rates do not belong in one number. Under
    "focused" the fix's files are the only files present, so fault localization is
    given away and the question is only "what change"; under "full" the agent has to
    find the fault first, which the source benchmark measures as a task in its own
    right and at which the best reported model reaches 45% Top-1. A pooled rate over a
    mixture is an average over two populations whose proportions are an artefact of
    which tasks have been re-materialised so far.
    """
    present = [c for c in ALL_CONDITIONS if any(r["condition"] == c for r in rows)]
    scopes: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        scope = _repo_scope(tasks_root / row["task_id"])
        scopes[scope][row["condition"]].append(row)
    if len(scopes) < 2:
        only = next(iter(scopes), "focused")
        print(f"\nAll tasks are repo_scope={only}; no scope split needed.")
        return

    print("\nBy repository scope -- these are different tasks and must not be pooled")
    print(f"  {'scope':<10}{'condition':<14}{'decided':>9}{'solved':>8}{'rate':>8}")
    for scope in sorted(scopes):
        for condition in present:
            group = scopes[scope].get(condition, [])
            scored = group if args.strict else [r for r in group if r["decided"]]
            if not scored:
                continue
            solved = sum(1 for r in scored if r["solved"])
            print(
                f"  {scope:<10}{condition:<14}{len(scored):>9}{solved:>8}"
                f"{solved / len(scored):>8.1%}"
            )
    print(
        "  Report these separately, or restrict the headline to one scope. Pooling them\n"
        "  is the mistake EVALUATION.md section 6 warns against."
    )


def command_report(args, runs_root: Path, tasks_root: Path) -> int:
    verdicts = _verdicts(args, runs_root, tasks_root)
    if not verdicts:
        print("No runs to score.")
        return 1

    rows = [v.as_dict() for v in verdicts]
    out = runs_root / f"verdicts_{args.agent}.jsonl"
    write_verdicts(out, verdicts)

    # Coverage before rates. The harness materialises a prompt and a pristine workspace
    # for every planned run, so `len(rows)` is the size of the *plan*, not of the
    # evidence. Printing a rate against it counts every cell nobody has run yet as a
    # failed repair. Each line below is a denominator someone might quote; the point of
    # showing them together is that the reader can see which one a rate is over.
    attempted = [r for r in rows if r.get("attempted", True)]
    timed_out = [r for r in attempted if not r["decided"] and "timeout" in r["reason"]]
    decided = [r for r in attempted if r["decided"]]
    print(f"Agent: {args.agent}")
    print(f"\n{'Coverage':<34}{'runs':>7}")
    print(f"  {'planned (cells materialised)':<32}{len(rows):>7}")
    print(f"  {'attempted (an agent ran)':<32}{len(attempted):>7}   {len(attempted)/len(rows):>6.1%} of plan")
    print(f"  {'killed by the timeout':<32}{len(timed_out):>7}")
    print(f"  {'decided by some oracle':<32}{len(decided):>7}")
    unattempted = len(rows) - len(attempted)
    if unattempted:
        print(
            f"\n  {unattempted} planned cells have not been run and are excluded from every rate\n"
            f"  below. They are missing data, not failed repairs; --strict does not fold\n"
            f"  them in, because the benchmark's convention covers undecidable attempts."
        )

    provenance = Counter(r["oracle"] for r in attempted)
    print("\nWhich oracle decided each run")
    for oracle in ("execution", "static", "judge", "none"):
        count = provenance.get(oracle, 0)
        if count:
            # Share of *attempted* runs, not of the plan: the plan's unrun cells have no
            # oracle and would silently deflate every share here.
            print(f"  {oracle:<11}{count:>5}  {count / len(attempted):>6.1%} of attempted")
    _report_model_consistency(attempted, runs_root, args)
    _report_judge_consistency(attempted, runs_root, args)

    if provenance.get("execution", 0) == 0:
        print("  !! no run was decided by executing the workflow. Every success below is")
        print("     inferred by a model, which is the weakest claim this pipeline can make.")

    print(f"\n{'condition':<14}{'runs':>6}{'decided':>9}{'solved':>8}{'rate':>8}{'unstable':>10}")
    by_cond: dict[str, list[dict]] = defaultdict(list)
    for row in attempted:
        by_cond[row["condition"]].append(row)
    for condition, group in sorted(by_cond.items()):
        scored = group if args.strict else [r for r in group if r["decided"]]
        solved = sum(1 for r in scored if r["solved"])
        rate = solved / len(scored) if scored else 0.0
        print(
            f"{condition:<14}{len(group):>6}{sum(1 for r in group if r['decided']):>9}"
            f"{solved:>8}{rate:>8.1%}"
            f"{sum(1 for r in group if r.get('unstable')):>10}"
        )

    _report_scope_split(attempted, tasks_root, args)

    # Keyed on the canonical arm: runs collected as `with_memory` are the same
    # treatment as `memory_k3` and belong in the same column. Keying on the raw name
    # left every task looking unpaired -- the analysis reported "no task was completed
    # under both conditions" over a set where every task was.
    control, memory_arm = PAIR
    per_task: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in attempted:
        if args.strict or row["decided"]:
            arm = canonical_condition(row["condition"])
            per_task[row["task_id"]][arm].append(bool(row["solved"]))

    analysis = paired_rate_analysis({t: dict(c) for t, c in per_task.items()})
    print("\nPaired analysis over tasks (the unit of independence, not runs)")
    if analysis["n_tasks"] == 0:
        print("  " + analysis["interpretation"])
    else:
        print(f"  tasks              {analysis['n_tasks']}   ({analysis['runs_per_task']} runs each)")
        print(f"  {control:<18} {analysis['rate_no_memory']:.1%}")
        print(f"  {memory_arm:<18} {analysis['rate_with_memory']:.1%}")
        ci = analysis["ci_difference"]
        print(f"  difference         {analysis['difference']:+.1%}  95% CI [{ci['low']:+.1%}, {ci['high']:+.1%}]")
        print(f"  tasks improved     {analysis['tasks_improved']} / worse {analysis['tasks_worsened']} / same {analysis['tasks_unchanged']}")
        print(f"\n  {analysis['interpretation']}")

        sd = per_task_sd(analysis["per_task_difference"])
        if sd > 0 and analysis["difference"] != 0:
            needed = tasks_for_power(analysis["difference"], sd)
            print(
                f"\n  To detect an effect of {analysis['difference']:+.1%} at 80% power you need "
                f"about {needed} paired tasks (per-task SD {sd:.3f}).\n"
                f"  You currently have {analysis['n_tasks']}."
            )
            for target in (0.03, 0.05, 0.08, 0.10):
                print(f"    to detect {target:.0%}: {tasks_for_power(target, sd):>5} tasks")

        # A task run once contributes a rate of 0 or 1 and moves the mean by 1/n_tasks on
        # a single Bernoulli draw; a task run ten times contributes an estimate. Averaging
        # them unweighted lets the thinnest evidence carry the effect, so the split below
        # says how much of the difference rests on tasks with too few runs to have a rate
        # at all.
        thin = {t: c for t, c in per_task.items()
                if min(len(c.get(control, [])), len(c.get(memory_arm, []))) < 3
                and c.get(control) and c.get(memory_arm)}
        thick = {t: c for t, c in per_task.items()
                 if t not in thin and c.get(control) and c.get(memory_arm)}
        if thin and thick:
            print(f"\n  Run balance: {len(thin)} paired tasks have <3 runs in an arm, {len(thick)} have 3+.")
            for label, subset in (("<3 runs/arm", thin), ("3+ runs/arm", thick)):
                sub = paired_rate_analysis({t: dict(c) for t, c in subset.items()})
                if sub["n_tasks"]:
                    print(
                        f"    {label:<13} n={sub['n_tasks']:<3} "
                        f"{control} {sub['rate_no_memory']:.1%}  {memory_arm} {sub['rate_with_memory']:.1%}  "
                        f"difference {sub['difference']:+.1%}"
                    )
            print(
                "    If the two lines disagree in sign or size, the pooled difference is a\n"
                "    statement about scheduling order, not about memory. Equalise runs per\n"
                "    task before quoting the pooled number."
            )

    print(f"\nPass@{args.k} over tasks finished in both conditions")
    scores: dict[str, list[float]] = defaultdict(list)
    for task, conditions in per_task.items():
        # Pairing is required on the two comparison arms only, so an arm present on
        # disk but never run cannot suppress the comparison that was. `CONDITIONS` was
        # the right set while it held exactly those two; with the K sweep it demands
        # all four and would report nothing until the whole grid was filled.
        if not all(conditions.get(c) for c in PAIR):
            continue
        for condition, group in conditions.items():
            if len(group) >= args.k:
                scores[condition].append(pass_at_k(len(group), sum(group), args.k))
    for condition in sorted(scores, key=lambda c: (condition_k(c), c)):
        values = scores[condition]
        print(f"  {condition:<14}{statistics.mean(values):.3f}  over {len(values)} tasks")

    # Every task is listed, including those with no attempted run and those whose runs
    # were all undecidable. A table that silently omits a task the reader cannot see is
    # a selection rule, and dropping tasks for a reason correlated with the outcome --
    # "it produced nothing judgeable" -- is the one to avoid.
    # One column per arm that actually has runs, so an arm cannot be run and then go
    # missing from the table that is supposed to show what was run.
    arms = [c for c in ALL_CONDITIONS if any(r["condition"] == c for r in attempted)]
    arms = arms or list(CONDITIONS)
    print("\nPer task (solved / decided, of attempted)")
    print(f"{'task':<28}" + "".join(f"{c:>16}" for c in arms) + f"{'scope':>9}{'oracle':>12}")
    for task in sorted({r["task_id"] for r in rows}):
        cells = []
        for condition in arms:
            group = [r for r in attempted if r["task_id"] == task and r["condition"] == condition]
            scored = group if args.strict else [r for r in group if r["decided"]]
            if scored:
                cells.append(f"{sum(1 for r in scored if r['solved'])}/{len(scored)}")
            elif group:
                cells.append(f"0/0 of {len(group)}")
            else:
                cells.append("not run")
        oracles = Counter(r["oracle"] for r in attempted if r["task_id"] == task)
        top = oracles.most_common(1)[0][0] if oracles else "-"
        scope = _repo_scope(tasks_root / task)
        print(f"{task:<28}" + "".join(f"{c:>16}" for c in cells) + f"{scope:>9}{top:>12}")

    print(f"\nWrote {out}")
    return 0


def command_agreement(args, runs_root: Path, tasks_root: Path) -> int:
    rows = [v.as_dict() for v in _verdicts(args, runs_root, tasks_root)]
    overall = judge_vs_execution(rows)

    print("Judge against full CI re-execution\n")
    if overall.n == 0:
        print(f"  {overall.interpretation}")
        print(
            "\n  Run:  python scripts/judge_runs.py --agent {a} --mode judge"
            "\n  then: python scripts/score_runs.py --agent {a} --mode execute"
            "\n  on the same runs, and this table becomes reportable.".format(a=args.agent)
        )
        return 1

    print(f"  runs compared      {overall.n}")
    print(f"  raw agreement      {overall.raw_agreement:.1%}")
    print(f"  Cohen's kappa      {overall.kappa:.3f}")
    print(f"  precision          {overall.precision:.3f}   (of runs the judge called repaired, share CI agreed)")
    print(f"  recall             {overall.recall:.3f}   (of runs CI passed, share the judge caught)")
    print(f"  judge rate         {overall.judge_rate:.1%}")
    print(f"  execution rate     {overall.execution_rate:.1%}")
    print(f"  judge bias         {overall.rate_bias:+.1%}")
    print(f"\n  false positives    {overall.judge_only}   (judge: repaired, CI: red)")
    print(f"  false negatives    {overall.execution_only}   (judge: not repaired, CI: green)")
    print(f"\n  {overall.interpretation}")

    print("\nWithin each condition (does the judge's error favour one arm?)")
    print(f"{'condition':<14}{'n':>5}{'kappa':>8}{'judge':>9}{'exec':>9}{'bias':>9}")
    per_condition = by_condition(rows)
    for condition, stats in per_condition.items():
        if stats.n == 0:
            print(f"{condition:<14}{0:>5}{'-':>8}{'-':>9}{'-':>9}{'-':>9}")
            continue
        print(
            f"{condition:<14}{stats.n:>5}{stats.kappa:>8.3f}"
            f"{stats.judge_rate:>9.1%}{stats.execution_rate:>9.1%}{stats.rate_bias:>+9.1%}"
        )
    biases = [s.rate_bias for s in per_condition.values() if s.n > 0]
    if len(biases) == 2:
        skew = biases[0] - biases[1]
        print(
            f"\n  Differential bias {skew:+.1%}. This is the number that matters: a judge "
            f"wrong\n  by the same amount in both arms shifts the rates and leaves their "
            f"difference intact."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score runs with execution first, a model only where it must be",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--mode", choices=("execute", "report", "agreement", "tokens"), default="report")
    parser.add_argument("--backend", default="github", help="github, local, local:docker, local:apptainer")
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--repos-dir", default=str(REPO_ROOT / "runs" / "_repos"))
    parser.add_argument("--owner", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--condition", choices=CONDITIONS, default=None)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--poll-timeout", type=int, default=3600)
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--usable-only",
        action="store_true",
        help="Only instances validate_instances.py proved red-before / green-after.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Count undecided runs as failures, as CI-Repair-Bench does for instances "
             "whose CI could not be triggered. Default drops them, which is the more "
             "informative denominator. Report both.",
    )
    parser.add_argument(
        "--agent-model",
        default="sonnet",
        help="Which model the agent ran, used only to price its estimated token use "
             "in --mode tokens. The runner does not record this, so it has to be told.",
    )
    args = parser.parse_args()

    runs_root = Path(args.runs_root).resolve()
    tasks_root = Path(args.tasks_root).resolve()
    if args.mode == "execute":
        return command_execute(args, runs_root, tasks_root)
    if args.mode == "tokens":
        return command_tokens(args, runs_root, tasks_root)
    if args.mode == "agreement":
        return command_agreement(args, runs_root, tasks_root)
    return command_report(args, runs_root, tasks_root)


if __name__ == "__main__":
    raise SystemExit(main())
