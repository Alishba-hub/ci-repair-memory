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
| `with_memory` | yes | yes | 3 earlier failures from **this** project |

The failing log is in both conditions because it is the problem statement, not memory.
The only thing that varies is the memory block.

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
│   ├── importer.py loader.py memory.py log_compressor.py prompt_builder.py
│   ├── oracle.py oracle_github.py oracle_local.py ci_outcome.py static_checks.py
│   ├── judge.py agreement.py evaluator.py stats.py tokens.py patchio.py
│   ├── workflow_std.py dashboard_state.py report.py ui.html
├── scripts/                   the individual steps run.py calls
├── results_archive/           frozen results from an earlier protocol; not current
└── vscode-extension/          drives GitHub Copilot, which has no CLI
```

`repo_before` is the broken state, `repo_after` the reference. A run succeeds when the
agent's fix removes the same root cause — not when the text matches.

## Main scripts

| | |
|---|---|
| `import_ci_repair_bench.py` | Builds task folders from the dataset |
| `materialize_repos.py` | Replaces gold-file trees with real checkouts, so localisation is part of the task |
| `audit_leakage.py` | **Proves the experiment is fair** — how much of each answer is visible in each condition |
| `validate_pipeline.py` | Calibrates the textual diagnostics and Pass@K. **Not** the judge |
| `validate_instances.py` | Proves each instance is red unpatched and green with the gold patch |
| `run_experiment.py` | Lays out prompts and workspaces; also the textual diagnostics |
| `auto_run.py` | Runs the agent over pending cells, in parallel, resumable |
| `judge_runs.py` | The model judge |
| `score_runs.py` | **The scorer.** Oracle precedence, coverage, rates, power, token accounting |
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
