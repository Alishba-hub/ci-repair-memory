"""The experimental design, in one place, so no script can quietly run a different one.

Every number the three research questions depend on lives here. They were spread across
argparse defaults, a hardcoded pair of condition names and two selection filters before,
which is how `--runs 10` in one script and `--runs 5` in another became possible: the
design was whatever the last command line said.

    RQ1  Does project memory improve bug-fixing success?
         30 tasks, 10 repositories, 3 error-type groups, memory vs no memory.
    RQ2  How consistent are agents across repeated runs?
         RUNS_PER_CONDITION identical runs of every task in every condition.
    RQ3  How does the amount of project history affect performance?
         The same tasks at K = 1, 3, 5 memory items, each item being one
         (failure log, gold patch) pair.

The three questions share one grid, deliberately. RQ1 is the K=0 vs K=RQ1_K contrast
inside it, RQ2 is the spread across the runs of a cell, RQ3 is the curve across K. Run
separately they would need three populations of tasks and no result could be carried
from one question to the next.
"""

from __future__ import annotations

# --- The grid ---------------------------------------------------------------------

#: Memory sizes probed by RQ3. Each item is one earlier failure: its log and the patch
#: that fixed it. The sets are nested -- the K=1 item is the first item of K=3, which is
#: the first three of K=5 -- so a difference between two K values is the extra history
#: and not a different selection of it.
K_VALUES: tuple[int, ...] = (1, 3, 5)

#: How many memory items each task stores on disk. It must be max(K_VALUES): prompts
#: take a prefix of this list, so storing fewer would silently truncate the largest arm.
MEMORY_SIZE: int = max(K_VALUES)

#: The memory arm RQ1 reports. K=3 is the midpoint of K_VALUES and was the size used in
#: the pilot, so the RQ1 number stays comparable with the earlier runs.
RQ1_K: int = 3

#: Repeated runs per task per condition (RQ2). Agents are non-deterministic; a single
#: run per cell measures one sample of a distribution and calls it the agent.
RUNS_PER_CONDITION: int = 5

#: Task population. 10 repositories x 3 tasks each.
#:
#: This is the design's intent and is deliberately NOT lowered to what the dataset
#: currently yields. Measured on the 567-row parquet, exactly ten repositories have
#: three or more K=5-eligible targets -- agno, aider, axolotl, browser-use, camel,
#: conan, crewai, docsgpt, litellm, taipy -- and the count does not improve at any
#: MAX_FILES ceiling (15, 20, 25, 30, 40 and 60 all give the same ten). So there is no
#: substitute bench: losing one repository costs a tenth of the study.
#:
#: One is currently lost. All three of camel's eligible instances (410, 419, 420) carry
#: a gold patch whose pre-image does not match the repository at `sha_fail` -- in 410,
#: the patch deletes `if board[a] in ("X", "O") and ...` where the commit actually holds
#: `if board[a] != " " and ...`. That is a defect in the dataset, not in the importer:
#: no patch tool can apply a hunk whose deleted line is absent, and fuzz relaxes context
#: only. The importable ceiling is therefore 9 x 3 = 27 until those rows are corrected
#: upstream or a tenth repository becomes eligible.
#:
#: The importer reports the shortfall loudly and records what it actually built in
#: `tasks/study_population.json`. Lowering N_REPOS to 9 here would make the harness
#: agree with itself by moving the target, which is the one thing this module exists to
#: prevent.
N_REPOS: int = 10
TASKS_PER_REPO: int = 3
N_TASKS: int = N_REPOS * TASKS_PER_REPO


# --- Instance eligibility ----------------------------------------------------------

#: A target needs at least this many strictly earlier, non-leaking failures in its own
#: project, because the K=5 arm has to be fillable for every task in the study. Filling
#: it for some tasks and not others would make the K curve a curve over a changing
#: population.
MIN_PRIOR: int = MEMORY_SIZE

#: Ceiling on the number of files the gold patch touches. Raised from 3 to 15 because
#: at 3 only six repositories in CI-Repair-Bench have three K=5-eligible targets, and
#: the design calls for ten. Measured on the 567-row dataset: 3 -> 6 repos, 10 -> 9,
#: 15 -> 11. This is the smallest value that admits the required ten with one to spare.
MAX_FILES: int = 15

#: Drop targets whose failure is purely formatting, linting or docstrings. They are the
#: majority of CI-Repair-Bench and their gold patch is often whitespace, so they measure
#: whether the agent ran a formatter rather than whether it can repair a build.
SEMANTIC_ONLY: bool = True

#: A memory item reproducing more than this fraction of the target's gold patch is
#: dropped. Being strictly earlier is not enough: in taipy an instance nine days older
#: already contains 97% of the later fix.
MAX_OVERLAP: float = 0.25


# --- The three problem groups ------------------------------------------------------

#: The "3 different problems" the 30 tasks are stratified over. Grouped rather than
#: taken as raw `error_type` labels because CI-Repair-Bench assigns several labels to
#: one instance and the thin labels (Type Checking Error: 2 rows in the whole dataset)
#: cannot carry a tenth of the study on their own.
#:
#: Order matters. A target carrying labels from two groups is assigned to the first
#: group listed here that matches, so the scarcer groups get first claim and the
#: stratification does not collapse into the largest one.
ERROR_GROUPS: dict[str, tuple[str, ...]] = {
    "test_assertion": (
        "Test Failure",
        "Assertion Error",
    ),
    "code_runtime": (
        "Runtime Error",
        "Syntax Error",
        "Type Checking Error",
    ),
    "dependency_env": (
        "Dependency Issues",
        "Package Installation Error",
        "Environment Error",
        "Configuration Error",
    ),
}

GROUP_NAMES: tuple[str, ...] = tuple(ERROR_GROUPS)


def error_group(error_types) -> str | None:
    """Which of the three problem groups an instance belongs to.

    Returns None for instances that are purely formatting or linting, which
    `SEMANTIC_ONLY` excludes anyway; the two rules agree by construction.
    """
    labels = set(error_types or [])
    for group, members in ERROR_GROUPS.items():
        if labels & set(members):
            return group
    return None


# --- The agents under evaluation ---------------------------------------------------

#: One entry per (harness, model) cell of the study. The harness is the scaffold that
#: reads the prompt and edits the workspace; the model is the LLM behind it. Both are
#: recorded per run, because "copilot" alone does not identify a condition once the
#: same harness is driven by more than one model, and results collected under different
#: models would otherwise pool into a single row.
#:
#: GitHub Copilot driven by three models. `gpt-5.4` and `gpt-5.4-mini` are the same
#: family at two sizes, so the pair isolates model capacity with the harness, the
#: prompts and the task population all held fixed; `claude-fable-5.1` is the
#: cross-vendor comparison. Add rows to extend the matrix; every script derives its
#: agent list from here, so nothing else needs editing.
#:
#: `model` must match the family string VS Code reports for the model, because the
#: extension pins `ciMemory.modelFamily` to it and refuses to run on a mismatch rather
#: than silently collecting a condition under whatever model the router chose.
AGENTS: tuple[dict[str, str], ...] = (
    {
        "name": "copilot-claude-fable-5.1",
        "harness": "copilot",
        "model": "claude-fable-5.1",
        "output_mode": "text",
    },
    {
        "name": "copilot-gpt-5.4",
        "harness": "copilot",
        "model": "gpt-5.4",
        "output_mode": "text",
    },
    {
        "name": "copilot-gpt-5.4-mini",
        "harness": "copilot",
        "model": "gpt-5.4-mini",
        "output_mode": "text",
    },
)

AGENT_NAMES: tuple[str, ...] = tuple(agent["name"] for agent in AGENTS)

#: The Copilot cells the VS Code runner accepts, each with the one model family it must
#: run with. Mirrors ALLOWED_AGENTS in vscode-extension/extension.js; the dashboard's
#: run buttons and scripts/run_copilot.py read it from here.
RUNNER_AGENTS: dict[str, str] = {
    "copilot-gpt-5.4": "gpt-5.4",
    "copilot-claude-fable-5.1": "claude-fable-5.1",
}

#: What the dashboard opens when no agent is named: the cell laid out under the current
#: design (4 arms x RUNS_PER_CONDITION runs). `claude-code` is the 10-run, two-arm pilot
#: and showing it by default made the current design look like it had not been applied.
DASHBOARD_AGENT: str = "copilot-gpt-5.4"


def agent_config(name: str) -> dict[str, str]:
    """The registered cell called `name`, or a bare record for an unregistered one.

    An unknown name is not an error. Exploratory runs live in `runs/<whatever>` and must
    still be scoreable; they simply carry no model attribution, which the CSV shows as
    an empty `model` column rather than by guessing one.
    """
    for agent in AGENTS:
        if agent["name"] == name:
            return dict(agent)
    return {"name": name, "harness": name, "model": "", "output_mode": "text"}


# --- Derived totals, for the plan the dashboard prints -----------------------------

def planned_runs_per_agent() -> int:
    """Cells one agent must fill: tasks x conditions x repeats."""
    from .prompt_builder import CONDITIONS

    return N_TASKS * len(CONDITIONS) * RUNS_PER_CONDITION


def planned_runs_total() -> int:
    return planned_runs_per_agent() * len(AGENTS)
