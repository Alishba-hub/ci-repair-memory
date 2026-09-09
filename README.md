# CI Memory Agents

Does giving an AI coding agent earlier failures and fixes from the same project improve
its ability to repair a failing CI build?

This is an experiment harness built around real CI failures from **CI-Repair-Bench**. It
materialises task data, constructs prompts under each condition, runs an agent against
fresh copies of the broken repository, and decides success through a precedence of
oracles.

> **Status: no effect has been detected, and the current rates are provisional.**
> Across the 135 runs decided so far the agent repairs roughly 57–62% of these builds
> either way. The per-task interval contains zero, what movement there is comes from
> tasks run once or twice, and the verdict set currently mixes two judge versions.
> **Read the correction notices at the top of [EXPERIMENT_REPORT.md](EXPERIMENT_REPORT.md)
> before quoting any number from this repository.**

---

## Start here

```powershell
cd D:\research\ci-memory-agents
pip install -r requirements.txt
python run.py
```

`run.py` asks which agent, which projects or how many tasks, and how many runs per
condition; shows what will happen; then does it. It finds the
agent binary itself and prints the equivalent flags so the next run needs no questions.

| Command | |
|---|---|
| `python run.py` | Ask what to run, then run it |
| `python run.py doctor` | Check this machine can run it |
| `python run.py tasks` | List the projects and pick what to run |
| `python run.py status` | Progress, and whether the result is defensible yet |
| `python run.py results` | The results table |
| `python run.py tokens` | What it has cost, and what the full study would cost |

Full operational detail, including the two steps that turn inferred results into measured
ones, is in **[HOW_TO_RUN.md](HOW_TO_RUN.md)**.

## The four documents

Each has one job. If they ever disagree, the one nearest the code wins.

| | |
|---|---|
| **README.md** | This file. What the project is and where things live |
| **[HOW_TO_RUN.md](HOW_TO_RUN.md)** | How to run it |
| **[EXPERIMENT_REPORT.md](EXPERIMENT_REPORT.md)** | The design, the findings, what they cannot claim, and what is missing |
| **[EVALUATION.md](EVALUATION.md)** | How correctness is decided and why — written to be lifted into a paper's evaluation section |

---

## The idea

Each task is one real failed CI workflow. The agent gets the repository at the failing
commit, the failing log, and the workflow definition, and must edit the files so the build
would pass.

| Condition | Repo at the break | Failing log | Memory block |
|---|---|---|---|
| `no_memory` | yes | yes | none |
| `memory_k1` | yes | yes | the 1 most recent earlier failure from **this** project |
| `memory_k3` | yes | yes | the 3 most recent earlier failures from **this** project |
| `memory_k5` | yes | yes | the 5 most recent earlier failures from **this** project |

Each memory item is one (failure log, gold patch) pair. The failing log of the *target*
is in every condition because it is the problem statement, not memory. The only thing
that varies is the memory block.

The K sets are nested: the K=1 item is the first item of K=3, which is the first three of
K=5. A difference between two arms is therefore the additional history and not a
different draw from it — without that, "more memory" would be confounded with "different
memory" and RQ3 would be unanswerable.

`no_memory` against `memory_k3` is the RQ1 contrast; the sweep across all four is RQ3.
Two further arms may appear in `runs/`: `with_memory`, the name `memory_k3` was collected
under before the sweep and analysed as the same treatment, and `foreign_memory`, a
placebo showing the same number of failures drawn from a *different* project. All six are
defined in `src/ci_memory_agents/prompt_builder.py`.

## The design

Everything the three research questions depend on lives in
`src/ci_memory_agents/design.py`, so no script can quietly run a different study:

| | |
|---|---|
| Tasks | 30 — 10 repositories x 3 tasks |
| Problem groups | 3 — test/assertion, code/runtime, dependency/environment |
| Runs per condition | 5 (RQ2) |
| Memory sizes | K = 1, 3, 5 (RQ3) |
| Cells per agent | 30 tasks x 4 arms x 5 runs = 600 |
| Agents | 3 — GitHub Copilot on `claude-fable-5.1`, `gpt-5.4`, `gpt-5.4-mini` |
| Cells in total | 600 x 3 agents = 1,800 |

`gpt-5.4` and `gpt-5.4-mini` are one family at two sizes, so the pair isolates model
capacity with harness, prompts and tasks held fixed; `claude-fable-5.1` is the
cross-vendor comparison.

Task selection is deterministic — no sampling, no seed — so the same parquet yields the
same 30 tasks on any machine. Which 30 they are is recorded in
`tasks/study_population.json`, written by the importer: `tasks/` accumulates folders
from earlier designs, and the harness lays out only what the manifest names.

The project explicitly audits whether the memory block contains the answer, and audits
localisation leakage — a prior fix touching the same file as the target's — separately,
because content overlap will never see it. Chronological ordering alone turned out not to
be sufficient, which is one of the pilot's real findings; see EXPERIMENT_REPORT §5.2.

## How success is decided

In precedence order (`src/ci_memory_agents/oracle.py`), with the deciding oracle recorded
on every verdict:

1. **static** — deterministic checks. Conclusive only *against* a patch: an empty patch,
   a file that no longer parses, a deleted test, a workflow edited to ignore itself.
2. **execution** — re-run the workflow, pass iff every check passes. This is
   CI-Repair-Bench's own oracle and what a headline number should mean.
   **It has not been run yet** — `ci_conclusion` is empty on every verdict on disk.
3. **judge** — a blind model verdict on whether the patch removes the same root cause as
   the reference fix. Every number in this repository currently comes from here, with no
   measured agreement against execution, which makes it an inference rather than a
   measurement.

`run.py results` prints which oracle decided what, so this is visible rather than assumed.

Textual measures — `exact_match`, `normalized_match`, file IoU/precision/recall,
`line_deviation_ratio` — are reported as diagnostics, never as the criterion. Across all
runs, 46 of 79 successful repairs took a route the maintainer did not take; text matching
called all 46 failures. That argument is EXPERIMENT_REPORT §6.

---

## Repository layout

```text
ci-memory-agents/
├── run.py                     the single entry point
├── requirements.txt
├── README.md · HOW_TO_RUN.md · EXPERIMENT_REPORT.md · EVALUATION.md
├── data/ci-repair-bench.parquet    raw dataset, 240 MB, gitignored
├── tasks/
│   ├── crb_<project>_<instance>/
│   │   ├── repo_before/            files at the failing commit
│   │   ├── repo_after/             reference post-fix snapshot
│   │   ├── gold_patch.diff         the maintainers' fix
│   │   ├── ci_logs/failed.compressed.log
│   │   ├── memory/                 3 earlier failures: prior_NN_*.log and .diff
│   │   ├── workflow.yml
│   │   └── metadata.json
│   └── demo_issue_00{1,2,3}/       tiny fixtures for smoke-testing the harness
├── runs/<agent>/<task>/<condition>/run_NN/     gitignored
│   ├── prompt.md · workspace/ · agent_response.md
│   ├── agent_meta.json             exit code, timings, sizes
│   ├── judgement.json              the verdict, with the judging prompt's hash
│   └── token_usage.json            what this run cost
├── src/ci_memory_agents/
│   ├── design.py                   the grid; every script reads it, none overrides it
│   ├── importer.py loader.py memory.py log_compressor.py prompt_builder.py
│   ├── oracle.py oracle_github.py oracle_local.py ci_outcome.py static_checks.py
│   ├── judge.py agreement.py evaluator.py stats.py tokens.py patchio.py
│   ├── workflow_std.py dashboard_state.py report.py ui.html
├── scripts/                   the individual steps run.py calls
├── results/                   the analysis snapshot, rewritten whole on every export
│   ├── runs.csv                    one row per run; everything else derives from it
│   ├── rq1_memory_effect.csv rq2_consistency.csv rq3_memory_size.csv
│   ├── by_error_group.csv by_repo.csv population.csv
│   ├── memory_history.csv          every (failure log, gold patch) pair, and the K arms it appears in
│   └── prompts.csv                 each arm's prompt, hashed and measured
├── results_archive/           frozen results from an earlier protocol; not current
└── vscode-extension/          drives GitHub Copilot, which has no CLI
```

`repo_before` is the broken state, `repo_after` the reference. A run succeeds when the
agent's fix removes the same root cause — not when the text matches.

## Main scripts

| | |
|---|---|
| `import_ci_repair_bench.py` | Builds task folders from the dataset, and writes `tasks/study_population.json` |
| `materialize_repos.py` | Replaces gold-file trees with real checkouts, so localisation is part of the task |
| `audit_leakage.py` | **Proves the experiment is fair** — how much of each answer is visible in each condition |
| `validate_pipeline.py` | Calibrates the textual diagnostics and Pass@K. **Not** the judge |
| `validate_instances.py` | Proves each instance is red unpatched and green with the gold patch |
| `run_experiment.py` | Lays out prompts and workspaces; also the textual diagnostics |
| `auto_run.py` | Runs the agent over pending cells, in parallel, resumable |
| `judge_runs.py` | The model judge |
| `score_runs.py` | **The scorer.** Oracle precedence, coverage, rates, power, token accounting |
| `export_results.py` | Writes `results/*.csv` — the analysis snapshot, no API key needed |
| `dashboard.py` | Browser view |

If you call these directly rather than through `run.py`, one rule matters: use
`score_runs.py --mode report`, **not** `judge_runs.py --mode report`. Only the first knows
the difference between a run that happened and a cell that was never run.

## Notes

- **Everything resumes.** Both runners skip any cell with an `agent_meta.json` and never
  overwrite a workspace someone edited by hand. Re-issue the same command to continue.
- **`runs/` is gitignored.** Results live on the machine that produced them. Copy the
  folder to share them.
- **Only two dependencies**, `pyarrow` and `PyYAML`. Everything else is the standard
  library, deliberately — this has to run unattended where installing things is a
  negotiation. Python 3.10+.
- **Known gaps** — no licence, no citation file, no recorded model id per run, and six
  measurements that do not exist yet — are listed explicitly in EXPERIMENT_REPORT §10b
  rather than left to be discovered.
