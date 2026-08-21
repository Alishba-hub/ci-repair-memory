# How to run everything

Always start here:

```powershell
cd D:\research\ci-memory-agents
```

## 1. Dashboard (see progress and results)

```powershell
python scripts\dashboard.py --port 8004 --agent claude-code
```

Then open <http://localhost:8004>. Switch agents with the dropdown at the top.
Leave the window open. `Ctrl+C` stops it.

## 2. Run Claude Code (automatic)

```powershell
python scripts\auto_run.py --preset claude --agent claude-code `
  --exe "C:\Users\admin\.vscode\extensions\anthropic.claude-code-2.1.233-win32-x64\resources\native-binary\claude.exe" `
  --limit 40 --parallel 6 --timeout 900
```

- `--limit` how many runs this time, `0` for all
- `--parallel` how many at once; 6 is a good default
- `--dry-run` to preview without running

## 3. Run Copilot (automatic)

`Ctrl+Shift+P` in VS Code:

1. **Developer: Reload Window**
2. **CI Memory: Run All Experiments**

Settings live under `Ctrl+,` -> search `ciMemory`.

## 4. Score

Success is decided by a judge that reads the failing log, the maintainer's patch and
the agent's patch, and answers whether the agent removed the same root cause. Text
comparison against the maintainer's commit cannot answer that; see "Metrics" in
`experiment_plan.md`.

```powershell
# judge every completed run (cached per run, so this is resumable and re-runs free)
python scripts\judge_runs.py --agent claude-code `
  --exe "C:\Users\admin\.vscode\extensions\anthropic.claude-code-2.1.233-win32-x64\resources\native-binary\claude.exe" `
  --model sonnet --parallel 8

# the results table
python scripts\judge_runs.py --agent claude-code --mode report

# sample to check by hand; agreement with the judge is reported next to any result
python scripts\judge_runs.py --agent claude-code --mode calibration --sample 20
```

`--force` re-judges runs that already have a verdict. Verdicts live in each run folder
as `judgement.json`.

For the textual diagnostics (file recall, line deviation, match against the gold text):

```powershell
python scripts\run_experiment.py --mode score --agent claude-code --k 1
```

## Resuming

**Everything resumes by itself.** Both runners skip any run that already has an
`agent_meta.json`, and never touch a workspace edited by hand. If a batch is
interrupted, run the same command again and it continues where it stopped.

To redo a single run, delete its `agent_meta.json`, or press **Undo this run** in
the dashboard.

## Before a long batch

Turn off sleep: Settings -> System -> Power -> put my device to sleep -> **Never**.
A sleeping machine kills runs mid-edit; those are recorded as timeouts and excluded
from scoring, so the work is wasted.

## Starting over

```powershell
# new tasks from the dataset
python scripts\import_ci_repair_bench.py --limit 24 --max-per-project 2

# regenerate prompts (text for Copilot, inplace for Claude/Cursor)
python scripts\run_experiment.py --mode prompts --agent copilot     --output-mode text    --runs 10
python scripts\run_experiment.py --mode prompts --agent claude-code --output-mode inplace --runs 10

# health checks
python scripts\audit_leakage.py
python scripts\validate_pipeline.py
```
