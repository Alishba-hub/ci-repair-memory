#!/usr/bin/env python3
"""One command to run the experiment. Start here.

    python run.py                 do everything that still needs doing, then report
    python run.py status          what is done, what is not
    python run.py doctor          check this machine can run it, fix nothing
    python run.py results         just print the results table
    python run.py dashboard       open the browser view

Everything is resumable and nothing is destructive: each step skips work that is
already finished, and no step will overwrite a run an agent has already done. If a
batch is interrupted, run the same command again.

Why this file exists
--------------------
Running the experiment used to take five commands in the right order, a hand-pasted
path to an agent binary, and three flags you had to know about or your results were
silently wrong -- the wrong task source pulled demo fixtures into the results tree,
and the wrong scorer could not tell a run that happened from one that never did.
Those are not choices a person should have to make correctly from memory. They are
defaults now, and the ones that remain are asked for in plain language.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
# `scripts/` too: the task-selection logic lives with the script that owns it, so that
# filtering means exactly the same thing here as it does when a script is called directly.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from ci_memory_agents import design
from ci_memory_agents.prompt_builder import CONDITIONS

TASK_SOURCE = "ci-repair-bench"

# ANSI, disabled when the terminal will not render it.
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def ok(text: str) -> str:
    return _c(text, "32")


def warn(text: str) -> str:
    return _c(text, "33")


def bad(text: str) -> str:
    return _c(text, "31")


def dim(text: str) -> str:
    return _c(text, "2")


def bold(text: str) -> str:
    return _c(text, "1")


def heading(text: str) -> None:
    print(f"\n{bold(text)}\n{dim('-' * len(text))}")


# --------------------------------------------------------------------------------
# Finding the agent
# --------------------------------------------------------------------------------

def find_claude() -> str | None:
    """Locate the Claude Code binary without making anyone paste a path.

    `claude` on PATH is the normal case. The VS Code extension ships its own copy and
    is often the only one installed, so the extension directories are searched too,
    newest version first -- the folder names sort lexically in version order well
    enough for this, and a wrong guess is caught immediately by `doctor`.
    """
    found = shutil.which("claude")
    if found:
        return found

    roots = [
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".vscode-insiders" / "extensions",
        Path.home() / ".cursor" / "extensions",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for ext in root.glob("anthropic.claude-code-*"):
            for name in ("claude.exe", "claude"):
                binary = ext / "resources" / "native-binary" / name
                if binary.is_file():
                    candidates.append(binary)
    if not candidates:
        return None
    return str(sorted(candidates, key=lambda p: p.parent.parent.parent.name)[-1])


# --------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------

def survey(agent: str) -> dict:
    """Count what is on disk, using the same definitions the scorer uses."""
    runs_root = REPO_ROOT / "runs" / agent
    tasks = sorted(p for p in (REPO_ROOT / "tasks").glob("crb_*") if p.is_dir())
    cells = list(runs_root.glob("*/*/run_*")) if runs_root.exists() else []
    cells = [c for c in cells if c.is_dir()]
    attempted = [c for c in cells if (c / "agent_meta.json").exists()]
    judged = [c for c in attempted if (c / "judgement.json").exists()]
    timed_out = [c for c in attempted if (c / "agent_timeout.txt").exists()]
    executed = [c for c in attempted if (c / "ci_outcome.json").exists()]
    validated = [t for t in tasks if (t / "ci_baseline.json").exists()]
    return {
        "agent": agent,
        "tasks": len(tasks),
        "cells": len(cells),
        "attempted": len(attempted),
        "judged": len(judged),
        "timed_out": len(timed_out),
        "needs_judging": len([c for c in attempted if c not in judged and c not in timed_out]),
        "executed": len(executed),
        "validated": len(validated),
    }


def bar(done: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return dim("-" * width)
    filled = round(width * done / total)
    return ok("#" * filled) + dim("." * (width - filled))


def _selection_flags(args) -> list[str]:
    """The task narrowing, in the form every script accepts.

    Built once and reused so that laying out cells, running the agent and scoring all
    act on the same set. A filter meaning different things in different steps would
    compare one population against another without saying so.
    """
    flags: list[str] = []
    if args.task_id:
        flags += ["--task-id", args.task_id]
    for project in args.project or []:
        flags += ["--project", project]
    if args.tasks:
        flags += ["--tasks", str(args.tasks)]
    return flags


def selected_tasks(args) -> list[str]:
    from run_experiment import select_tasks  # noqa: PLC0415

    tasks = select_tasks(
        REPO_ROOT / "tasks",
        source=TASK_SOURCE,
        task_id=args.task_id,
        projects=args.project,
        limit=args.tasks,
    )
    return [task.task_id for task in tasks]


def command_tasks(args) -> int:
    """What is available to run, and what each project has already done."""
    from run_experiment import project_of, select_tasks  # noqa: PLC0415

    every = select_tasks(REPO_ROOT / "tasks", source=TASK_SOURCE)
    filtering = bool(args.project or args.tasks or args.task_id)
    # With no filters every task is selected, so marking them all says nothing. The
    # column only earns its space when it distinguishes something.
    chosen = set(selected_tasks(args)) if filtering else set()
    runs_root = REPO_ROOT / "runs" / args.agent

    by_project: dict[str, list] = {}
    for task in every:
        by_project.setdefault(project_of(task.task_id), []).append(task)

    heading(f"{len(every)} tasks across {len(by_project)} projects")
    print(f"  {'project':<24}{'tasks':>6}{'run':>6}{'judged':>8}   {'task ids'}")
    for project in sorted(by_project):
        ids = [t.task_id for t in by_project[project]]
        cells = [c for tid in ids for c in runs_root.glob(f"{tid}/*/run_*") if c.is_dir()]
        run_n = sum(1 for c in cells if (c / "agent_meta.json").exists())
        judged = sum(1 for c in cells if (c / "judgement.json").exists())
        mark = ok(" *") if any(i in chosen for i in ids) else "  "
        short = ", ".join(i.split("_")[-1] for i in ids)
        print(f"{mark}{project:<24}{len(ids):>6}{run_n:>6}{judged:>8}   {dim(short)}")

    if args.project or args.tasks or args.task_id:
        print(f"\n  {ok('*')} marks the {len(chosen)} tasks your current filters select.")
    print(f"""
  Pick what to run:
    python run.py --project agno --project httpx     {dim('named projects')}
    python run.py --tasks 6                          {dim('6 tasks, spread across projects')}
    python run.py --project conan --tasks 1          {dim('both, combined')}
    python run.py --task-id crb_aider_97             {dim('one exact task')}
    python run.py --tasks 6 --runs 5                 {dim('6 tasks, 5 runs per condition')}""")
    return 0


# --------------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------------

def _ask(question: str, default: str = "") -> str:
    """One line of input, with the default shown and Enter accepting it."""
    suffix = f" [{bold(default)}]" if default else ""
    try:
        answer = input(f"  {question}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _ask_int(question: str, default: int, low: int, high: int) -> int:
    while True:
        raw = _ask(question, str(default))
        try:
            value = int(raw)
        except ValueError:
            print(bad(f"    '{raw}' is not a number."))
            continue
        if not low <= value <= high:
            print(bad(f"    Pick between {low} and {high}."))
            continue
        return value


def _ask_yes(question: str, default: bool = False) -> bool:
    while True:
        raw = _ask(f"{question} (y/n)", "y" if default else "n").lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(bad("    Answer y or n."))


def _choose(title: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """A numbered menu. One choice, returned as a 1-based index.

    Numbered rather than arrow-driven on purpose: arrow keys need raw terminal mode,
    which behaves differently in PowerShell, Windows Terminal, the VS Code terminal and
    over SSH. A number and Enter works identically everywhere, including when the
    output is piped.
    """
    print(f"\n  {bold(title)}")
    for index, (label, note) in enumerate(options, start=1):
        marker = ok(">") if index == default else " "
        print(f"   {marker} {index}. {label:<34}{dim(note)}")
    while True:
        raw = _ask("Choose", str(default))
        try:
            value = int(raw)
        except ValueError:
            print(bad(f"    '{raw}' is not one of the numbers above."))
            continue
        if 1 <= value <= len(options):
            return value
        print(bad(f"    Pick between 1 and {len(options)}."))


def _choose_projects(rows: list[tuple[str, int, int]]) -> list[str] | None:
    """Multi-select over projects. Returns None for "all of them".

    Accepts `3`, `3,7,9`, `3-6`, `all`, or a project name typed out, because people
    reach for whichever of those is closest to hand.
    """
    print(f"\n  {bold('Which projects?')}")
    print(f"   {dim('#   project                  tasks   runs done')}")
    for index, (project, tasks, done) in enumerate(rows, start=1):
        print(f"   {index:>2}. {project:<24}{tasks:>5}{done:>11}")
    print(f"\n   {dim('Enter numbers (3), lists (3,7,9), ranges (3-6), a name, or all')}")

    names = [row[0] for row in rows]
    while True:
        raw = _ask("Projects", "all").lower()
        if raw in ("all", "*", ""):
            return None
        picked: list[str] = []
        bad_bits: list[str] = []
        for bit in raw.replace(" ", ",").split(","):
            if not bit:
                continue
            if "-" in bit and all(part.strip().isdigit() for part in bit.split("-", 1)):
                start, end = (int(part) for part in bit.split("-", 1))
                for index in range(start, end + 1):
                    if 1 <= index <= len(names):
                        picked.append(names[index - 1])
                    else:
                        bad_bits.append(str(index))
            elif bit.isdigit():
                index = int(bit)
                if 1 <= index <= len(names):
                    picked.append(names[index - 1])
                else:
                    bad_bits.append(bit)
            else:
                matches = [n for n in names if n.lower() == bit] or [
                    n for n in names if bit in n.lower()
                ]
                if len(matches) == 1:
                    picked.append(matches[0])
                elif matches:
                    print(bad(f"    '{bit}' matches {', '.join(matches)} -- be more specific."))
                    bad_bits.append(bit)
                else:
                    bad_bits.append(bit)
        if bad_bits:
            print(bad(f"    Not recognised: {', '.join(bad_bits)}"))
            continue
        if not picked:
            print(bad("    Nothing selected."))
            continue
        return sorted(dict.fromkeys(picked))


def command_pick(args) -> int:
    """Ask what to run, then run it.

    This is what a bare `python run.py` does at a terminal. Everything it asks maps to
    a flag, and the flags it chose are printed at the end, so the first run teaches the
    command for the next one instead of hiding it.
    """
    from run_experiment import project_of, select_tasks  # noqa: PLC0415

    print(f"\n{bold('  CI Memory Agents')}")
    print(dim("  Answer or press Enter for the default. Ctrl+C to leave; nothing is saved until you confirm."))

    every = select_tasks(REPO_ROOT / "tasks", source=TASK_SOURCE)
    if not every:
        print(bad("\n  No tasks built. Run:  python scripts/import_ci_repair_bench.py --limit 24 --max-per-project 2"))
        return 1

    # 1. agent
    agent_choice = _choose(
        "Which agent?",
        [
            ("Claude Code", "runs automatically"),
            ("Copilot", "you drive it in VS Code"),
        ],
        default=1,
    )
    args.agent = "claude-code" if agent_choice == 1 else "copilot"

    runs_root = REPO_ROOT / "runs" / args.agent
    by_project: dict[str, list] = {}
    for task in every:
        by_project.setdefault(project_of(task.task_id), []).append(task)
    rows = []
    for project in sorted(by_project):
        ids = [t.task_id for t in by_project[project]]
        done = sum(
            1
            for tid in ids
            for cell in runs_root.glob(f"{tid}/*/run_*")
            if (cell / "agent_meta.json").exists()
        )
        rows.append((project, len(ids), done))

    # 2. scope
    scope = _choose(
        "How much do you want to run?",
        [
            (f"Everything", f"all {len(every)} tasks"),
            ("Pick projects", "choose from a list"),
            ("A number of tasks", "spread across projects"),
            ("Just one task", "smallest possible check"),
        ],
        default=1,
    )
    args.project, args.tasks, args.task_id = None, 0, None
    if scope == 2:
        args.project = _choose_projects(rows)
    elif scope == 3:
        args.tasks = _ask_int(f"How many tasks (1-{len(every)})", min(6, len(every)), 1, len(every))
    elif scope == 4:
        args.project = _choose_projects(rows)
        args.tasks = 1

    # 3. repetition
    print(f"\n  {bold('How many runs per condition?')}")
    print(dim("   These agents expose no temperature, so repeating a task is the only way to"))
    print(dim("   see how consistent it is. Tasks run once contribute a coin flip, not a rate;"))
    print(dim("   3 or more is where a task starts to have a repair rate at all."))
    args.runs = _ask_int("Runs per condition (1-20)", design.RUNS_PER_CONDITION, 1, 20)


    # 5. speed
    args.parallel = _ask_int("\n  How many runs at once (1-12)", 6, 1, 12)

    # 6. confirm
    chosen = selected_tasks(args)
    arms = len(CONDITIONS)
    planned = len(chosen) * arms * args.runs
    conditions = ("no_memory", "with_memory")
    already = sum(
        1
        for task_id in chosen
        for condition in conditions
        for index in range(1, args.runs + 1)
        if (runs_root / task_id / condition / f"run_{index:02d}" / "agent_meta.json").exists()
    )
    todo = max(0, planned - already)

    heading("Here is what will happen")
    print(f"  agent                  {args.agent}")
    print(f"  tasks                  {len(chosen)} of {len(every)}")
    for task_id in chosen[:8]:
        print(f"                           {dim(task_id)}")
    if len(chosen) > 8:
        print(dim(f"                           ... and {len(chosen) - 8} more"))
    print(f"  conditions             {', '.join(conditions)}")
    print(f"  runs per condition     {args.runs}")
    print(f"  cells in total         {planned}")
    print(f"  already run            {already}")
    print(f"  {bold('the agent will run')}     {bold(str(todo))}")
    if todo and args.agent != "copilot":
        print(dim(f"  roughly {todo * 200 / max(1, args.parallel) / 60:.0f} minutes at {args.parallel} at a time"))

    flags = " ".join(_selection_flags(args) + ["--runs", str(args.runs),
                                               "--parallel", str(args.parallel)]

                     + ([f"--agent {args.agent}"] if args.agent != "claude-code" else []))
    print(f"\n  {dim('Same thing without the questions next time:')}")
    print(f"  {bold('python run.py ' + flags)}")

    if not _ask_yes("\n  Start", default=True):
        print("  Cancelled. Nothing was changed.")
        return 0
    return command_run(args)


# --------------------------------------------------------------------------------
# Running steps
# --------------------------------------------------------------------------------

def call(script: str, *args: str, quiet: bool = False) -> int:
    command = [sys.executable, str(REPO_ROOT / script), *args]
    if not quiet:
        print(dim("   $ python " + script + " " + " ".join(args)))
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=quiet,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode


def capture(script: str, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


# --------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------

def command_doctor(args) -> int:
    heading("Can this machine run the experiment?")
    problems: list[str] = []
    notes: list[str] = []

    print(f"  {ok('OK'):<6} Python {sys.version.split()[0]}")

    tasks = list((REPO_ROOT / "tasks").glob("crb_*"))
    if tasks:
        print(f"  {ok('OK'):<6} {len(tasks)} tasks built under tasks/")
    else:
        print(f"  {bad('NO'):<6} no tasks built")
        problems.append("Build the tasks:  python scripts/import_ci_repair_bench.py --limit 24 --max-per-project 2")

    exe = args.exe or find_claude()
    if exe:
        print(f"  {ok('OK'):<6} agent binary  {dim(exe)}")
    else:
        print(f"  {warn('--'):<6} no Claude Code binary found automatically")
        notes.append("Pass --exe \"C:\\path\\to\\claude.exe\", or run Copilot through the VS Code extension.")

    code, _ = capture("scripts/audit_leakage.py")
    if code == 0:
        print(f"  {ok('OK'):<6} leakage audit passes -- no task hands the memory arm the answer")
    else:
        print(f"  {bad('NO'):<6} leakage audit FAILS")
        problems.append("Inspect it:  python scripts/audit_leakage.py")

    code, _ = capture("scripts/validate_pipeline.py")
    print(f"  {(ok('OK') if code == 0 else bad('NO')):<6} scoring diagnostics calibrated")
    if code != 0:
        problems.append("Inspect it:  python scripts/validate_pipeline.py")

    have_gh = all(os.environ.get(v) for v in ("GITHUB_TOKEN", "GITHUB_USERNAME", "BENCHMARK_OWNER"))
    if have_gh:
        print(f"  {ok('OK'):<6} GitHub credentials set -- the execution oracle can run")
    else:
        missing = [v for v in ("GITHUB_TOKEN", "GITHUB_USERNAME", "BENCHMARK_OWNER") if not os.environ.get(v)]
        print(f"  {warn('--'):<6} execution oracle unavailable, missing {', '.join(missing)}")
        notes.append(
            "The execution oracle is the only thing that can turn these results from\n"
            "     inferred into measured. Set those three, then:  python run.py verify"
        )

    if problems:
        heading("Fix these first")
        for line in problems:
            print(f"  {line}")
        return 1

    if notes:
        heading("Worth knowing")
        for line in notes:
            print(f"  {line}")

    print(f"\n{ok('Ready.')} Run the experiment with:  {bold('python run.py')}")
    return 0


# --------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------

def command_status(args) -> int:
    s = survey(args.agent)
    heading(f"Progress for {s['agent']}")
    planned = s["cells"] or 1
    print(
        f"  cells laid out    {s['cells']:>4}          "
        + dim(f"{s['tasks']} tasks x {len(CONDITIONS)} conditions x N runs")
    )
    print(f"  agent has run     {s['attempted']:>4} / {s['cells']:<5} {bar(s['attempted'], planned)}")
    print(f"  judged            {s['judged']:>4} / {s['attempted']:<5} {bar(s['judged'], s['attempted'] or 1)}")
    if s["timed_out"]:
        print(f"  {dim('timed out')}         {s['timed_out']:>4}          {dim('excluded -- a killed run is a partial answer')}")

    heading("Is the result defensible yet?")
    checks = [
        (s["attempted"] > 0, "runs exist", "python run.py"),
        (s["needs_judging"] == 0 and s["judged"] > 0, "everything judged", "python run.py judge"),
        (s["validated"] > 0, "instances proven red-before / green-after", "python run.py verify"),
        (s["executed"] > 0, "success decided by executing CI, not inferred", "python run.py verify"),
    ]
    for passed, label, fix in checks:
        mark = ok("yes") if passed else bad(" no")
        print(f"  {mark}  {label:<46}{'' if passed else dim(fix)}")

    unmet = [c for c in checks if not c[0]]
    if unmet:
        print(f"\n  {warn('Until those are met')}, results are inferred by a model, not measured.")
        print(f"  Next step:  {bold(unmet[0][2])}")
    else:
        print(f"\n  {ok('All checks met.')}  python run.py results")
    return 0


# --------------------------------------------------------------------------------
# the main pipeline
# --------------------------------------------------------------------------------

def is_copilot(agent: str) -> bool:
    """Is this agent driven by the VS Code extension rather than a CLI?

    Matched on prefix because the folder name carries the model
    (`copilot-gpt-5.4-mini`), and every one of those is still Copilot.
    """
    return agent == "copilot" or agent.startswith("copilot-")


def command_run(args) -> int:
    before = survey(args.agent)

    exe = args.exe or find_claude()
    if not exe and not is_copilot(args.agent):
        print(bad("\nNo Claude Code binary found."))
        print("  Pass one with --exe, or check what is wrong with:  python run.py doctor")
        return 1

    chosen = selected_tasks(args)
    if not chosen:
        print(bad("\nNo tasks match those filters.  python run.py tasks"))
        return 1
    total = len(list((REPO_ROOT / "tasks").glob("crb_*")))
    arms = len(CONDITIONS)
    planned = len(chosen) * arms * args.runs

    conditions = ("no_memory", "with_memory")

    def _done(task_id: str) -> int:
        """Attempted cells among the ones this command would lay out.

        Scoped to `run_01..run_N` in the conditions being used, not to everything the
        task has ever had. Counting all of them makes `--runs 5` on a task already run
        ten times report more work done than planned, which reads as a bug.
        """
        root = REPO_ROOT / "runs" / args.agent / task_id
        return sum(
            1
            for condition in conditions
            for index in range(1, args.runs + 1)
            if (root / condition / f"run_{index:02d}" / "agent_meta.json").exists()
        )

    if args.project or args.tasks or args.task_id or args.dry_run:
        heading(f"Running {len(chosen)} of {total} tasks")
        for task_id in chosen:
            done = _done(task_id)
            print(f"  {task_id:<32}{dim(f'{done} runs already done') if done else ''}")
        print(dim(f"\n  {len(chosen)} tasks x {arms} conditions x {args.runs} runs = {planned} cells"))

    if args.dry_run:
        already = sum(_done(task_id) for task_id in chosen)
        todo = max(0, planned - already)
        heading("Dry run -- nothing was changed")
        print(f"  cells planned          {planned}")
        print(f"  already run            {already}")
        print(f"  the agent would run    {bold(str(todo))}")
        if todo:
            print(dim(f"  roughly {todo * 200 / max(1, args.parallel) / 60:.0f} minutes "
                      f"at {args.parallel} at a time"))
        print(f"\n  Go ahead with the same command minus {bold('--dry-run')}.")
        return 0

    heading("1/4  Checking the experiment is fair")
    if call("scripts/audit_leakage.py", quiet=True) != 0:
        print(bad("  Leakage audit failed. Stopping -- a leaking task would fake a positive result."))
        print("  See what:  python scripts/audit_leakage.py")
        return 1
    print(f"  {ok('OK')}  no task hands the memory arm its own answer")

    heading("2/4  Laying out the runs")
    prompt_args = [
        "--mode", "prompts",
        "--agent", args.agent,
        "--source", TASK_SOURCE,
        "--output-mode", "text" if args.agent == "copilot" else "inplace",
        "--runs", str(args.runs),
        "--refresh-workspaces",
    ]
    prompt_args += _selection_flags(args)
    if call("scripts/run_experiment.py", *prompt_args, quiet=True) != 0:
        return 1
    # Report the *selection*, not the whole run tree. `survey` counts every cell under
    # runs/<agent>, which for a filtered run is mostly other tasks: saying "485 cells
    # ready" after asking for one task is true of the directory and useless as an answer
    # to what was just laid out.
    laid = survey(args.agent)
    mine_done = sum(_done(task_id) for task_id in chosen)
    print(f"  {ok('OK')}  {planned} cells for the tasks you chose, {mine_done} already run")
    if laid["cells"] > planned:
        print(dim(f"        ({laid['cells']} cells exist under runs/{args.agent} in total, "
                  f"{laid['attempted']} of them run — the rest are other tasks)"))

    # Prefix, not equality. The agent folder carries the model name so two models cannot
    # share results (`copilot-gpt-5.4-mini`), and an `== "copilot"` test then falls
    # through to the CLI runner and quietly runs a completely different agent into a
    # folder labelled for this one. That happened; it is the exact contamination the
    # per-model folder exists to prevent.
    if is_copilot(args.agent):
        heading("3/4  Preparing VS Code")
        installed, detail = install_extension()
        print(f"  {(ok('OK') if installed else bad('NO')):<6} extension  {dim(detail)}")
        wrote, note = write_vscode_settings({
            "ciMemory.repoRoot": str(REPO_ROOT),
            "ciMemory.agent": args.agent,
            "ciMemory.delaySeconds": 2,
            "ciMemory.maxRuns": 0,
        })
        print(f"  {(ok('OK') if wrote else warn('--')):<6} settings   {dim(note)}")
        print(f"  {bold('Quit VS Code completely and reopen it')}, then press Ctrl+Shift+P:")
        if len(chosen) == 1:
            print(f"    {bold('CI Memory: Run A Single Task')}  ->  pick {bold(chosen[0])}")
            print(dim("\n  Use 'Run A Single Task', not 'Run All Experiments': the latter runs"))
            print(dim(f"  every pending cell under runs/{args.agent}, not only the task you chose."))
        else:
            print(f"    {bold('CI Memory: Run All Experiments')}")
            pending_all = laid["cells"] - laid["attempted"]
            pending_mine = planned - mine_done
            if pending_all > pending_mine:
                # Only worth saying when the folder holds work beyond this selection.
                # Warning that 75 cells is "not only the 75 you selected" is noise.
                print()
                print(dim(f"  Note: that runs every pending cell under runs/{args.agent} -- "
                          f"{pending_all} of them, not only the {pending_mine} you selected."))
                print(dim("  Use 'Run A Single Task' to stay inside the selection, or set"))
                print(dim("  ciMemory.maxRuns to cap it."))
            else:
                print()
                print(dim(f"  That is exactly the {pending_mine} cells you selected; this"))
                print(dim("  folder holds nothing else pending."))
        print(f"\n  Come back and finish with:  "
              f"{bold(f'python run.py judge --agent {args.agent}')}")
        return 0

    todo = laid["cells"] - laid["attempted"]
    heading(f"3/4  Running the agent on {todo} remaining cells")
    if todo == 0:
        print(f"  {ok('OK')}  nothing left to run")
    else:
        minutes = todo * 200 / max(1, args.parallel) / 60
        print(f"  {dim(f'roughly {minutes:.0f} minutes at {args.parallel} at a time. Ctrl+C is safe: everything resumes.')}")
        auto = [
            "--agent", args.agent,
            "--preset", args.preset,
            "--parallel", str(args.parallel),
            "--timeout", str(args.timeout),
            "--exe", exe,
        ]
        # `--tasks N` is not passed on: the layout step already applied it, so the
        # runner's job is simply to run every cell that exists. Passing a count to both
        # would re-narrow an already-narrowed set and silently drop tasks.
        if args.task_id:
            auto += ["--task-id", args.task_id]
        elif args.tasks:
            auto += ["--task-id", ",".join(chosen)]
        for project in args.project or []:
            auto += ["--project", project]
        if args.limit:
            auto += ["--limit", str(args.limit)]
        call("scripts/auto_run.py", *auto)

    heading("4/4  Judging and reporting")
    return _judge_and_report(args, exe)


def _judge_and_report(args, exe: str | None) -> int:
    state = survey(args.agent)
    if state["needs_judging"]:
        print(f"  judging {state['needs_judging']} runs")
        judge = ["--agent", args.agent, "--model", args.judge_model, "--parallel", str(args.parallel)]
        if exe:
            judge += ["--exe", exe]
        call("scripts/judge_runs.py", *judge, quiet=True)
    else:
        print(f"  {ok('OK')}  every run already judged")

    print()
    call("scripts/score_runs.py", "--agent", args.agent, "--mode", "report", quiet=False)
    _closing_advice(args)
    return 0


def _closing_advice(args) -> None:
    s = survey(args.agent)
    heading("What this result is worth")
    if s["executed"] == 0:
        print(f"  {warn('Every success above was inferred by a model, not observed.')}")
        print("  The benchmark's own oracle is to re-run the failing workflow and require")
        print("  every check to pass. Nothing here has been decided that way yet.")
        print(f"\n  Next:  {bold('python run.py verify')}   {dim('(needs a GitHub token; costs Actions minutes, not tokens)')}")
    elif s["control_attempted"] == 0:
        print("  Memory prompts are 2.25x longer than no-memory ones, so an effect could be")
        print("  the extra context rather than the memory itself.")
        print(f"\n  Next:  {bold('python run.py control')}")
    else:
        print(f"  {ok('Execution-backed.')} Check per-condition agreement:")
        print(f"  {bold('python run.py verify --agreement-only')}")
    print(f"\n  Full picture any time:  {bold('python run.py status')}")


# --------------------------------------------------------------------------------
# sub-commands
# --------------------------------------------------------------------------------

def command_judge(args) -> int:
    return _judge_and_report(args, args.exe or find_claude())


def command_results(args) -> int:
    code = call("scripts/score_runs.py", "--agent", args.agent, "--mode", "report", quiet=False)
    _closing_advice(args)
    return code


def command_verify(args) -> int:
    load_secrets()
    missing = [v for v in ("GITHUB_TOKEN", "GITHUB_USERNAME") if not os.environ.get(v)]
    if missing and not args.agreement_only:
        heading("The execution oracle needs GitHub credentials")
        print("  This is the step that turns inferred results into measured ones. It re-runs")
        print("  each failing workflow with the agent's patch applied and requires every")
        print("  check to pass -- the benchmark's own oracle. It costs GitHub Actions")
        print("  minutes, which are free on public repositories, and no model tokens.")
        print(f"\n  Missing: {', '.join(missing)}")
        print(f"\n  Store them once with:  {bold('python run.py login')}")
        print(dim("  The token needs `repo` and `workflow` scope. It is kept in a"))
        print(dim("  gitignored .secrets.json, entered without echoing, rather than"))
        print(dim("  retyped into every new terminal -- which is how tokens end up"))
        print(dim("  pasted somewhere they get recorded."))
        return 1

    if not args.agreement_only:
        heading("1/3  Proving each instance is red before repair and green with the gold patch")
        print(dim("  Two Actions runs per instance, once, cached. Instances failing either are"))
        print(dim("  excluded from both arms -- the check the benchmark's own harness skips."))
        validate = ["--mode", "validate", "--parallel", str(min(args.parallel, 4))]
        if args.task_id:
            validate += ["--task-id", args.task_id]
        call("scripts/validate_instances.py", *validate)
        # The table of what is now usable, printed after the runs it depends on.
        call("scripts/validate_instances.py", "--mode", "report")

        heading("2/3  Re-executing CI with each candidate patch")
        execute = ["--agent", args.agent, "--mode", "execute",
                   "--parallel", str(args.parallel), "--backend", args.backend]
        # `--limit` matters more here than anywhere else: each run is a real CI build
        # taking minutes, so "try four first" is the difference between finding a
        # misconfiguration in ten minutes and finding it after 480 builds.
        if args.limit:
            execute += ["--limit", str(args.limit)]
        execute += _selection_flags(args)[:2] if args.task_id else []
        call("scripts/score_runs.py", *execute)

    heading("3/3  How much the judge agreed with real CI")
    call("scripts/score_runs.py", "--agent", args.agent, "--mode", "agreement")
    print(dim("\n  Read the per-condition column. A judge wrong by the same amount in both arms"))
    print(dim("  shifts both rates and leaves their difference intact; one wrong only under"))
    print(dim("  with_memory manufactures the effect. Target kappa >= 0.6 on >= 50 runs."))
    return 0




def command_tokens(args) -> int:
    """What this has cost, what the reductions saved, and what the full study would cost."""
    return call("scripts/score_runs.py", "--agent", args.agent, "--mode", "tokens", quiet=False)


# --------------------------------------------------------------------------------
# Copilot setup
# --------------------------------------------------------------------------------

SECRETS_FILE = REPO_ROOT / ".secrets.json"


def load_secrets() -> dict:
    """GitHub credentials stored once, in a gitignored file beside the repo.

    Environment variables win when set, so a CI job or a one-off shell can still
    override. The file exists because the alternative is retyping a token into every new
    terminal, and the thing people do instead is paste it somewhere it gets recorded.
    """
    stored: dict = {}
    if SECRETS_FILE.exists():
        try:
            stored = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stored = {}
    for key in ("GITHUB_TOKEN", "GITHUB_USERNAME", "BENCHMARK_OWNER"):
        if os.environ.get(key):
            stored[key] = os.environ[key]
        elif stored.get(key):
            os.environ[key] = str(stored[key])
    return stored


def save_secrets(values: dict) -> None:
    """Write the credentials file, readable only by this user where the OS allows."""
    stored = load_secrets()
    stored.update({k: v for k, v in values.items() if v})
    SECRETS_FILE.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    try:
        SECRETS_FILE.chmod(0o600)
    except OSError:
        pass


def vscode_dirs() -> tuple[Path, Path]:
    """Where VS Code keeps extensions and user settings on this machine."""
    if sys.platform == "win32":
        return Path.home() / ".vscode" / "extensions", Path(
            os.environ.get("APPDATA", Path.home() / "AppData/Roaming")
        ) / "Code" / "User" / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / ".vscode" / "extensions", (
            Path.home() / "Library/Application Support/Code/User/settings.json"
        )
    return Path.home() / ".vscode" / "extensions", (
        Path.home() / ".config/Code/User/settings.json"
    )


def install_extension() -> tuple[bool, str]:
    """Install the VS Code extension, busting the manifest cache.

    VS Code caches an extension's `package.json` and a same-version reinstall does not
    reliably invalidate it -- a new setting can sit on disk and never appear in the
    settings UI. Bumping the patch version changes the folder name too, so it is scanned
    as a genuinely new extension. That is the whole reason this function edits a version
    number rather than just copying files.
    """
    source = REPO_ROOT / "vscode-extension"
    manifest = source / "package.json"
    if not manifest.exists():
        return False, "no vscode-extension/package.json in this repository"

    spec = json.loads(manifest.read_text(encoding="utf-8"))
    extensions, _ = vscode_dirs()
    if not extensions.is_dir():
        return False, f"VS Code extensions folder not found at {extensions}"

    # Bump only when the code actually differs from what is installed. Bumping every
    # time would be correct but hostile: each new version is a new folder, and a new
    # folder means another full VS Code restart. Reinstalling identical files earns
    # nobody a restart.
    installed = sorted(extensions.glob(f"{spec['publisher']}.*"))
    unchanged = (
        len(installed) == 1
        and (installed[0] / "extension.js").is_file()
        and (installed[0] / "extension.js").read_bytes()
        == (source / "extension.js").read_bytes()
        and (installed[0] / "package.json").is_file()
        and json.loads((installed[0] / "package.json").read_text(encoding="utf-8")) == spec
    )
    if unchanged:
        return True, f"v{spec['version']} already installed, unchanged"

    major, minor, patch = (int(part) for part in spec["version"].split("."))
    spec["version"] = f"{major}.{minor}.{patch + 1}"
    manifest.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    code = (source / "extension.js").read_text(encoding="utf-8")
    (source / "extension.js").write_text(
        re.sub(r'(EXTENSION_VERSION = ")[^"]+(")', rf'\g<1>{spec["version"]}\g<2>', code, count=1),
        encoding="utf-8",
    )

    for stale in installed:
        shutil.rmtree(stale, ignore_errors=True)
    target = extensions / f"{spec['publisher']}.{spec['name']}-{spec['version']}"
    shutil.copytree(source, target)
    return True, f"v{spec['version']} installed -- restart VS Code"


def write_vscode_settings(values: dict) -> tuple[bool, str]:
    """Put the ciMemory.* settings into the user's settings.json.

    Written rather than asked for, because every one of them is derivable: the repo root
    is where this script lives, the agent is what was selected, and the model is what the
    experiment needs pinned. Making a person type five settings correctly by hand is not
    a design, it is a way to collect typos.

    Existing unrelated settings are preserved. Comments in settings.json are not -- VS
    Code allows them and json does not -- so the file is left alone if it will not parse,
    with instructions instead.
    """
    _, settings_path = vscode_dirs()
    if not settings_path.parent.is_dir():
        return False, f"VS Code user folder not found at {settings_path.parent}"

    existing: dict = {}
    if settings_path.exists():
        text = settings_path.read_text(encoding="utf-8")
        try:
            existing = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return False, (
                f"{settings_path} has comments or trailing commas and cannot be edited "
                f"safely. Add these by hand:\n"
                + "\n".join(f'      "{k}": {json.dumps(v)},' for k, v in values.items())
            )
        settings_path.with_suffix(".json.bak").write_text(text, encoding="utf-8")

    changed = {k: v for k, v in values.items() if existing.get(k) != v}
    if not changed:
        return True, "already correct"
    existing.update(values)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return True, ", ".join(f"{k.split('.')[-1]}={v}" for k, v in changed.items())


def command_setup(args) -> int:
    """Get Copilot ready in one command: install the extension, write the settings."""
    heading("Setting up Copilot")

    okay, detail = install_extension()
    print(f"  {(ok('OK') if okay else bad('NO')):<6} extension  {dim(detail)}")
    if not okay:
        return 1

    # One results folder per model, derived rather than remembered.
    #
    # Runs from two models in the same folder are indistinguishable in a rate, and the
    # `resumable` behaviour makes it worse: a second model silently fills only the cells
    # the first had not reached, so the mix ends up *uneven across arms* and the
    # difference between conditions becomes partly a difference between models. That is
    # not a hypothetical -- it happened here, 20 gpt-4o-mini runs and 5 from a second
    # model, split 8/2 and 7/3 across the two arms. Deriving the folder from the model
    # makes it impossible rather than merely discouraged.
    agent = args.agent
    if args.model and agent == "copilot":
        agent = f"copilot-{args.model}"

    settings = {
        "ciMemory.repoRoot": str(REPO_ROOT),
        "ciMemory.agent": agent,
        "ciMemory.delaySeconds": 2,
        "ciMemory.maxRuns": 0,
    }
    if args.model:
        settings["ciMemory.modelFamily"] = args.model
        # A family whose id is "auto" is Copilot's router. Allowed, because sometimes it
        # is the only capable model, but every run records what it actually got and
        # `results` reports whether that varied.
        settings["ciMemory.allowRouterModel"] = True

    okay, detail = write_vscode_settings(settings)
    print(f"  {(ok('OK') if okay else bad('NO')):<6} settings   {dim(detail)}")
    if agent != args.agent:
        print(f"  {ok('OK'):<6} results    {dim(f'runs/{agent}/ -- one folder per model, so two models cannot mix')}")
    args.agent = agent
    if not okay:
        print(f"\n{detail}")
        return 1

    if not args.model:
        print(f"\n  {warn('Model not set.')} Pick one after restarting:")
        print("    Ctrl+Shift+P -> CI Memory: List Available Copilot Models")
        print(f"    then re-run:  {bold('python run.py setup --model <family>')}")

    heading("Two steps left, and they have to be done by hand")
    print(f"  1.  {bold('Quit VS Code completely')} and reopen it.")
    print(dim("      Not Reload Window: VS Code only rescans the extensions folder on a"))
    print(dim("      full restart, which is why a new setting can fail to appear."))
    print(f"  2.  {bold('Ctrl+Shift+P')} -> {bold('CI Memory: Run A Single Task')}")
    print(f"\n  Then come back and run:  {bold(f'python run.py judge --agent {args.agent}')}")
    return 0


def command_login(args) -> int:
    """Store the GitHub credentials the execution oracle needs, once.

    The token is read with getpass so it is never echoed to the terminal and never
    lands in shell history -- the two places a pasted secret usually ends up.
    """
    import getpass  # noqa: PLC0415

    heading("GitHub credentials for the execution oracle")
    print(dim("  Needed to fork the projects, push each candidate patch, and read the"))
    print(dim("  resulting Actions run. Stored in .secrets.json, which is gitignored."))
    print(dim("  Make the token at https://github.com/settings/tokens/new with the"))
    print(dim("  `repo` and `workflow` scopes."))
    print()

    existing = load_secrets()
    username = args.username or input(
        f"  GitHub username [{existing.get('GITHUB_USERNAME', '')}]: "
    ).strip() or existing.get("GITHUB_USERNAME", "")
    if not username:
        print(bad("  A username is required."))
        return 1

    have = "GITHUB_TOKEN" in existing
    prompt = "  Token (hidden; Enter keeps the stored one): " if have else "  Token (hidden): "
    token = getpass.getpass(prompt).strip() or existing.get("GITHUB_TOKEN", "")
    if not token:
        print(bad("  A token is required."))
        return 1
    if not token.startswith(("ghp_", "github_pat_")):
        print(warn("  That does not look like a GitHub token; storing it anyway."))

    save_secrets({"GITHUB_TOKEN": token, "GITHUB_USERNAME": username})
    print()
    print(f"  {ok('OK')}  stored for {username} in {SECRETS_FILE.name}")
    print()
    print(f"  Now run:  {bold(f'python run.py verify --agent {args.agent} --limit 4')}")
    return 0


def command_dashboard(args) -> int:
    print(f"Opening the dashboard at {bold('http://localhost:' + str(args.port))}")
    print(dim("Leave this window open. Ctrl+C stops it."))
    return call("scripts/dashboard.py", "--port", str(args.port), "--agent", args.agent)


def command_clean(args) -> int:
    """Delete unattempted cells so the next layout is rebuilt from scratch."""
    runs_root = REPO_ROOT / "runs" / args.agent
    victims = [
        c for c in runs_root.glob("*/*/run_*")
        if c.is_dir() and not (c / "agent_meta.json").exists()
    ]
    kept = len(list(runs_root.glob("*/*/run_*"))) - len(victims)
    if not victims:
        print(f"Nothing to clean. {kept} cells have been run and are never touched.")
        return 0
    print(f"This deletes {len(victims)} cells no agent has run.")
    print(f"{ok(str(kept))} cells that were run are kept, along with their results.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 0
    for cell in victims:
        shutil.rmtree(cell, ignore_errors=True)
    print(f"Removed {len(victims)} unattempted cells.")
    return 0


# --------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "Run the CI memory experiment. With no arguments it does whatever still "
            "needs doing and prints the results; it is safe to re-run at any time."
        ),
        epilog=(
            "examples:\n"
            "  python run.py                     run everything that is not done yet\n"
            "  python run.py --quick             a 2-task smoke test, about 15 minutes\n"
            "  python run.py status              what is done and what is missing\n"
            "  python run.py doctor              check this machine first\n"
            "  python run.py verify              decide success by really running CI\n"
            "  python run.py results             just print the table\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--agent", default="claude-code", help="which agent (default: claude-code)")
    parser.add_argument(
        "--runs",
        type=int,
        default=design.RUNS_PER_CONDITION,
        help=f"repeated runs per condition (default: {design.RUNS_PER_CONDITION})",
    )
    parser.add_argument("--parallel", type=int, default=6, help="runs at once (default: 6)")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per run (default: 900)")
    parser.add_argument("--exe", default=None, help="path to the agent binary; found automatically if omitted")
    parser.add_argument("--preset", default="claude", help="agent CLI preset")
    parser.add_argument("--username", default=None, help="GitHub username for `login`")
    parser.add_argument("--model", default=None, metavar="FAMILY",
                        help="Copilot model family for `setup`, e.g. gpt-5.4-mini")
    parser.add_argument("--judge-model", default="sonnet", help="model used for judging")
    parser.add_argument("--task-id", default=None, help="exact task id, or a comma-separated list")
    parser.add_argument(
        "--project",
        action="append",
        default=None,
        metavar="NAME",
        help="only this project, e.g. --project agno. Repeat for several. `python run.py tasks` lists them.",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=0,
        metavar="N",
        help="use at most N tasks, spread across projects rather than taken in order",
    )
    parser.add_argument("--limit", type=int, default=0, help="stop after N agent runs")
    parser.add_argument("--quick", action="store_true", help="2 runs per condition on one task, to check the setup works")
    parser.add_argument("--port", type=int, default=8004, help="dashboard port")
    parser.add_argument("--backend", default="github", help="execution backend: github or local")
    parser.add_argument("--agreement-only", action="store_true", help="skip execution, just report judge agreement")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show which tasks would run and how long it would take, then stop",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "pick", "setup", "login", "status", "doctor", "tasks", "tokens", "judge", "results", "verify", "dashboard", "clean"),
        help="what to do (default: run)",
    )
    args = parser.parse_args()

    if args.quick:
        args.runs = 2
        args.task_id = args.task_id or "crb_aider_97"
        print(dim(f"Quick mode: {args.runs} runs per condition on {args.task_id}."))

    # A bare `python run.py` at a terminal asks what to run rather than assuming.
    # Any explicit selection means the caller already knows what they want, and a
    # non-interactive shell must never block on input -- that would hang a batch job.
    explicit = any([args.project, args.tasks, args.task_id,
                    args.quick, args.dry_run, args.yes])
    if args.command == "run" and not explicit and sys.stdin.isatty():
        args.command = "pick"

    handlers = {
        "run": command_run,
        "status": command_status,
        "tasks": command_tasks,
        "tokens": command_tokens,
        "setup": command_setup,
        "login": command_login,
        "pick": command_pick,
        "doctor": command_doctor,
        "judge": command_judge,
        "results": command_results,
        "verify": command_verify,
        "dashboard": command_dashboard,
        "clean": command_clean,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print(f"\n\n{warn('Stopped.')} Nothing was lost -- run the same command again to continue.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
