# CI Memory Agents

This project tests a simple but important research question:

Does giving an AI coding agent access to earlier failures and fixes from the same project improve its ability to repair a failing CI build?

The repository is an experiment harness built around real GitHub issue/CI failures from CI-Repair-Bench. It materialises task data, constructs prompts under two conditions, runs an agent against fresh copies of the buggy repository, and scores the result against a reference fix.

## Core concept

Each task is a real failed CI workflow. The model is given:

- the repository snapshot at the failing commit (`repo_before`)
- the failing log / workflow information
- optionally, a memory block containing earlier failures and their fixes from the same project

The experiment compares two conditions:

- `no_memory`: the model sees the failing repo and log, but not earlier project history
- `with_memory`: the model sees the same failing repo and log plus prior issue-fix examples from the same project

The target failing log is included in both conditions, because it is the problem statement. The only difference is whether the prompt includes historical memory. The project also includes leakage checks to ensure the memory block does not contain the answer directly.

## Why this project exists

Most benchmark tasks ask a model to fix a single issue. This project studies whether project-specific incident history helps on the next similar failure. In other words, the question is not just "can the model patch the code?" but "can it use accumulated prior repair experience from the same codebase?"

## Repository structure

```text
ci-memory-agents/
├── README.md
├── HOW_TO_RUN.md
├── experiment_plan.md
├── EXPERIMENT_REPORT.md
├── START_HERE.bat
├── .gitignore
├── report_claude-code.html
├── research_meeting_reuse_notes.md
├── results_archive/
│   └── results_v1_no_test_edits.jsonl
├── runs/
│   ├── copilot/
│   └── claude-code/
│       └── <task_id>/
│           ├── no_memory/
│           │   └── run_01/
│           │       ├── prompt.md
│           │       ├── workspace/
│           │       ├── agent_response.md
│           │       └── agent_meta.json
│           └── with_memory/
├── scripts/
│   ├── import_ci_repair_bench.py
│   ├── audit_leakage.py
│   ├── validate_pipeline.py
│   ├── run_experiment.py
│   ├── auto_run.py
│   ├── dashboard.py
│   ├── judge_runs.py
│   └── make_report.py
├── src/
│   └── ci_memory_agents/
│       ├── __init__.py
│       ├── dashboard_state.py
│       ├── evaluator.py
│       ├── importer.py
│       ├── judge.py
│       ├── loader.py
│       ├── log_compressor.py
│       ├── memory.py
│       ├── prompt_builder.py
│       ├── report.py
│       ├── response_parser.py
│       ├── stats.py
│       └── ui.html
├── tasks/
│   ├── demo_issue_001/
│   ├── demo_issue_002/
│   ├── demo_issue_003/
│   └── crb_<project>_<instance>/
│       ├── repo_before/
│       ├── repo_after/
│       ├── gold_patch.diff
│       ├── ci_logs/
│       ├── memory/
│       ├── workflow.yml
│       └── metadata.json
└── vscode-extension/
    ├── extension.js
    ├── package.json
    └── README.md
```

## Task format

Each task lives under `tasks/` and follows this structure:

```text
tasks/crb_<project>_<instance>/
├── repo_before/         files at the failing commit
├── repo_after/          gold post-fix repository snapshot
├── gold_patch.diff      reference fix
├── ci_logs/
│   └── failed.compressed.log
├── memory/              earlier failures from same project and their patches
│   ├── prior_00_*.log
│   ├── prior_00_*.diff
│   └── ...
├── workflow.yml
├── metadata.json
└── ...
```

`repo_before` is the buggy state; `repo_after` is the reference state. A run is considered successful when the agent fix repairs the same root cause, not just when the text matches the patch verbatim.

## Experimental setup

### Data source

The project uses CI-Repair-Bench, which contains real CI failure tasks from open-source projects. The repo materialises these tasks and stores them locally in `tasks/`.

### Conditions

For each task, the harness creates fresh run folders under `runs/<agent>/<task_id>/` for each condition:

- `no_memory`
- `with_memory`

A task may be repeated multiple times (`run_01`, `run_02`, ...) to estimate robustness and pass@k performance.

### Prompt construction

The prompt builder assembles a task description for the agent. It may include:

- project context
- failing CI log
- root cause description
- repository files
- historical memory items

This is controlled in `src/ci_memory_agents/prompt_builder.py` and `src/ci_memory_agents/memory.py`.

### Leakage control

The project explicitly audits whether the memory block contains the gold fix or too much of the answer. The script `scripts/audit_leakage.py` checks overlap between the target fix and earlier memory items and flags contamination.

### Validation

Before trusted measurements, the project validates the pipeline:

```powershell
python scripts\validate_pipeline.py
```

This checks that the scoring logic distinguishes:

- a correct fix
- a no-op
- a partial fix

## Running the experiment

The project supports a browser dashboard and direct CLI automation.

### Option 1: dashboard (recommended for manual or mixed runs)

```powershell
cd D:\research\ci-memory-agents
python scripts\dashboard.py --port 8004 --agent copilot
```

Then open the local browser page. The dashboard:

- shows task status
- lists pending runs
- lets you open the prompt and workspace
- lets you score a task
- watches whether a run has already been completed

This is the easiest way to run a task manually while keeping the experimental state organised.

### Option 2: automatic CLI runner for Claude Code

Claude Code is run through the CLI with `scripts/auto_run.py`.

```powershell
cd D:\research\ci-memory-agents
python scripts\auto_run.py --preset claude --agent claude-code --limit 4
```

Useful flags:

```powershell
python scripts\auto_run.py --preset claude --agent claude-code --dry-run
python scripts\auto_run.py --preset claude --agent claude-code --limit 40 --parallel 6 --timeout 900
```

- `--dry-run`: preview what would run without launching the agent
- `--limit`: cap the number of runs
- `--parallel`: run multiple tasks concurrently
- `--timeout`: max time per run

The runner writes:

- `prompt.md`
- `workspace/`
- `agent_response.md`
- `agent_meta.json`

A run with `agent_meta.json` is treated as complete and skipped on later runs.

## Copilot workflow

Copilot is not run through a CLI here. The project uses the VS Code extension in `vscode-extension/` and the Language Model API.

### Install and use the extension

1. Open the repo in VS Code.
2. Load the extension from `vscode-extension/`.
3. Restart VS Code.
4. Open the command palette with `Ctrl+Shift+P`.
5. Run:

- `CI Memory: List Available Copilot Models`
- `CI Memory: Run A Single Task`
- `CI Memory: Run All Experiments`

The extension writes run output into `runs/copilot/...` and skips any run already completed.

### Important note

The design intentionally uses a single-shot model call rather than a full multi-step agent loop. This keeps both conditions comparable. The experiment asks whether historical CI memory helps the model, not whether a separate tool-use agent is better than another one.

## Claude workflow

The Claude workflow is intended for CLI use, and the script is set up for repeated runs and prompt generation.

Example:

```powershell
python scripts\auto_run.py --preset claude --agent claude-code --exe "C:\path\to\claude.exe" --limit 10
```

This sends the generated prompt to Claude, lets it edit the run workspace in place, and stores the raw response and metadata.

## Prompt generation and scoring pipeline

### 1. Materialise the benchmark tasks

```powershell
python scripts\import_ci_repair_bench.py --limit 24 --max-per-project 2
```

This downloads the dataset and creates task folders under `tasks/`.

### 2. Generate experiment prompts

```powershell
python scripts\run_experiment.py --mode prompts --agent copilot --runs 10
python scripts\run_experiment.py --mode prompts --agent claude-code --runs 10
```

This writes prompt files and a fresh workspace copy per run.

### 3. Run the model on each workspace

For Copilot, use the VS Code extension.

For Claude, use:

```powershell
python scripts\auto_run.py --preset claude --agent claude-code --limit 10
```

### 4. Score the completed workspaces

```powershell
python scripts\run_experiment.py --mode score --agent copilot --k 3
python scripts\run_experiment.py --mode score --agent claude-code --k 3
```

This reads each run workspace, compares it against the repaired repository and the gold patch, and writes a JSONL summary to `runs/results_<agent>.jsonl`.

### 5. Judge runs

The judge provides a root-cause assessment rather than a pure text match:

```powershell
python scripts\judge_runs.py --agent claude-code --mode report
```

This summarizes the verdicts and can be used to calibrate decisions.

## Main scripts

- `scripts/import_ci_repair_bench.py` — creates local task data from the benchmark
- `scripts/audit_leakage.py` — checks memory leakage and contamination
- `scripts/validate_pipeline.py` — sanity-checks the evaluation pipeline
- `scripts/run_experiment.py` — generates prompts and scores outputs
- `scripts/auto_run.py` — launches automated CLI agents like Claude Code
- `scripts/dashboard.py` — browser UI for interactive experiment management
- `scripts/judge_runs.py` — assesses whether a patch fixes the root cause
- `scripts/make_report.py` — builds aggregate result reports

## Metrics

The project records multiple metrics, including:

- `exact_match`
- `normalized_match`
- `file_iou`
- `file_precision`
- `file_recall`
- `line_deviation_ratio`
- `Pass@K`

The key evaluation is not just raw textual similarity. Because benchmark patches often include unrelated refactors and release-note changes, the project uses a judge-based assessment and additional diagnostics to separate a real fix from a lucky or deceptive textual match.

## Demo tasks

The repo also contains a few tiny demo tasks:

- `demo_issue_001`
- `demo_issue_002`
- `demo_issue_003`

These are useful for smoke testing the harness without depending on the full benchmark dataset.

## Quick start

```powershell
cd D:\research\ci-memory-agents

# health checks
python scripts\audit_leakage.py
python scripts\validate_pipeline.py

# generate prompts
python scripts\run_experiment.py --mode prompts --agent copilot --runs 5

# run Copilot in VS Code via extension
# then score
python scripts\run_experiment.py --mode score --agent copilot --k 3

# or run Claude Code automatically
python scripts\auto_run.py --preset claude --agent claude-code --limit 5
```

## Notes

- Runs are resumable: completed work is skipped automatically.
- Manual edits to a workspace are protected to avoid overwriting real work.
- The dashboard and run folders keep the experiment reproducible and auditable.

This project is designed to study AI repair under historical memory, not just raw bug-fix ability. Its core value is that it makes the comparison explicit, repeatable, and inspectable.
