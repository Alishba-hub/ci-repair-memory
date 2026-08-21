# CI memory agents

An experiment harness for testing whether historical CI/CD logs help an AI coding
agent repair a failing build. Tasks are built from real CI failures in
[CI-Repair-Bench](https://huggingface.co/datasets/ci-benchmark-user/ci-repair-bench).

## Design

Every task is one real CI failure. The agent sees the repository at `sha_fail` and the
failing log, and must edit the files so the workflow passes. Two conditions:

| Condition | Repository | Failing log | Earlier failures in the same project |
|---|---|---|---|
| `no_memory` | yes | yes | no |
| `with_memory` | yes | yes | yes, with the patches that fixed them |

The failing log is in **both** conditions because it is the problem statement.
Withholding it would leave no issue to solve. The conditions differ only in the
memory block, which the leakage audit verifies.

## Running it automatically (no copy-paste)

**Copilot** — install the extension in `vscode-extension/`, restart VS Code, then
`Ctrl+Shift+P` -> **CI Memory: Run All Experiments**. It calls Copilot through the
VS Code Language Model API, writes each reply into the run workspace, and skips runs
that are already done. See `vscode-extension/README.md`.

**Claude Code, Cursor, Aider** — these have CLIs, so they can be driven directly:

```bash
python scripts/auto_run.py --preset claude --agent claude-code --limit 4
python scripts/auto_run.py --preset cursor --agent cursor --limit 4
python scripts/auto_run.py --preset claude --dry-run          # list without running
```

Any other agent works via `--command "yourtool --prompt {prompt_file}"`. Use
`--mode text` when the tool prints the answer, `--mode inplace` when it edits files
itself. Generate that agent's prompts first with
`--mode prompts --agent <name>`.

Both paths write `agent_response.md` and `agent_meta.json` next to each run, so results
stay auditable and reruns are skipped automatically.

## Running it by hand (easy way)

Double-click **`START_HERE.bat`**, or:

```bash
python scripts/dashboard.py
```

A browser opens at `http://localhost:8000` with the experiment runner: it shows which
runs are finished, hands you the next one, copies the prompt with one click, opens the
workspace in VS Code, and scores the run when you are done. No terminal needed after
that. Results update live and only count runs you have actually finished.

## Pipeline (command line)

```bash
# 1. Materialise tasks from CI-Repair-Bench (downloads a 240 MB parquet on first run)
python scripts/import_ci_repair_bench.py --limit 24 --max-per-project 2

# 2. Confirm no condition can read the answer
python scripts/audit_leakage.py

# 3. Confirm the scorer separates a correct fix from no fix
python scripts/validate_pipeline.py

# 4. Emit prompts and per-run workspaces
python scripts/run_experiment.py --mode prompts --agent copilot --runs 10

# 5. After the agent has edited the workspaces, score them
python scripts/run_experiment.py --mode score --agent copilot --k 3
```

Step 4 writes `runs/<agent>/<task>/<condition>/run_NN/` containing `prompt.md` and a
`workspace/` copy of the buggy repository. Point your agent at the workspace, give it
`prompt.md`, and let it edit the workspace in place. Step 5 scores whatever is there.

## Task layout

```
tasks/crb_<project>_<instance>/
  repo_before/      files at sha_fail
  repo_after/       gold post-fix state
  gold_patch.diff   the reference patch
  ci_logs/failed.compressed.log
  memory/           earlier failures: prior_NN_<id>.log and .diff
  workflow.yml
  metadata.json
```

## What each script guarantees

- `audit_leakage.py` reports, per task, the fraction of gold-patch lines visible in
  each condition's prompt. `no_memory` must be 0%.
- `validate_pipeline.py` scores three synthetic submissions. An oracle submission must
  score 1.00 exact, a no-op must score 0.00, and a half-fix must land between them.

## Metrics

`exact_match` and `normalized_match` (whitespace/comment insensitive, as in CI-Bench's
`evaluate.sh`), `file_iou` / `file_precision` / `file_recall` and
`line_deviation_ratio` (from Learning to Commit), and an unbiased `Pass@K`.

Scoring is deliberately not byte-exact. CI-Repair-Bench gold patches are whole commit
diffs, so they routinely carry release notes and unrelated refactors alongside the
real fix; file-level IoU degrades gracefully where `filecmp` reports a flat zero.

## Demo tasks

`demo_issue_001` to `demo_issue_003` are small hand-written fixtures for smoke-testing
the harness without network access. They are not part of any result.
