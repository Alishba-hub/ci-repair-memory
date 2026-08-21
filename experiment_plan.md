# Experiment plan

## Research question

Does giving an AI coding agent historical CI/CD logs from the same project improve its
ability to repair a failing CI build?

## Hypothesis

An agent repairs more failures, and more consistently, when it can see how the same
project's earlier CI failures were diagnosed and fixed.

## Conditions

| Condition | Repository at `sha_fail` | Target failing log | Earlier failures + their patches |
|---|---|---|---|
| `no_memory` | yes | yes | no |
| `with_memory` | yes | yes | yes |

**The target failing log appears in both conditions.** It is the problem statement, not
memory. Withholding it would leave the agent with no issue to solve and would compare
"repair a stated bug" against "find a bug", which is a different question. The
conditions differ only in the memory block.

## Dataset

CI-Repair-Bench: 567 CI failure instances, all Python, across 103 projects, commits
from Oct 2023 to Nov 2025. Fields: `sha_fail`, `sha_success`, `workflow`, `logs`,
`diff`, `changed_files`, `error_type`, `commit_date`.

Confirmed properties that shaped this design:

1. **There is no post-fix passing log.** All 567 `logs` entries carry failure markers.
   `sha_success` is a commit SHA, not a log. A passing log requires re-running the
   workflow on a fork, which is what the benchmark's own `run_benchmark.py` does.
2. **Logs cannot be pasted into a prompt.** Median 125K characters (~31K tokens), p90
   7.3M, max 52M. `src/ci_memory_agents/log_compressor.py` reduces them ~33x to a
   3,000-character budget while retaining an explicit failure signal in 81/81 sampled
   instances.
3. **Group by `repo_name`, not `repo_owner`.** Rows come from benchmark-owned forks, so
   `agno` appears under three owners. Grouping by owner splits one project's history
   and inflates the project count from 103 to 139.
4. **Logs were regenerated, not archived.** Log timestamps are 2026 while commits are
   2023-2025, so the workflows were re-run on forks. Do not describe them in the paper
   as the original historical builds.

## Task selection

Applied in `src/ci_memory_agents/memory.py`:

- **Temporal split.** A task is eligible only if its project has >=3 strictly earlier
  failures. Memory is the 3 most recent of those. 464/567 instances have at least one
  earlier failure; 347 have at least three.
- **Semantic failures only.** Targets failing purely for formatting or linting are
  dropped. Those are 58% of the benchmark (Code Linting 208, Code Formatting 121) and
  their gold patch is often a whitespace edit, which measures a formatter rather than
  repair ability.
- **Small patches.** Gold patch touches <=3 files.
- **Project balancing.** At most 2 tasks per project, round-robin. `agno` alone
  contributes 84 of 567 instances; taking candidates in order would make this a study
  of `agno`.

This yields 93 eligible instances; 24 are currently materialised across 21 projects.

## Leakage control

**Temporal ordering alone is not sufficient**, which is a finding in its own right.
Two contamination channels were found and are now blocked:

1. **Recurring patches.** In `taipy`, instance 440 predates target 439 by nine days yet
   already contains 97% of its gold patch. Chronology admits it; content must exclude it.
2. **Diff context lines.** In `axolotl`, a line the gold patch introduces appears as an
   unchanged *context* line in an earlier patch. An added-lines-only check reports 0%
   overlap while the agent can still read the answer.

Memory items are therefore rejected when more than 25% of the target's gold added lines
appear anywhere in the item's rendered text (patch **and** compressed log).

`scripts/audit_leakage.py` reports residual overlap per task. Current state: `no_memory`
is 0% on every task; 3 of 24 tasks retain 8-12% in `with_memory`, all below threshold.
Those residuals are project conventions such as a recurring `setuptools = "<81"` pin,
which is arguably the signal the memory condition is meant to supply. Where the line
falls between "useful precedent" and "answer key" is a judgement call that should be
stated as a threat to validity and settled with a sensitivity run over `--max-overlap`.

## Agents

Copilot first, since access already exists. Then Claude Code and Cursor. For Cursor,
log which model `auto` selected per run; model choice is otherwise an uncontrolled
variable.

## Protocol

For each task and each condition, run the agent 10 times on the same prompt against a
fresh copy of `repo_before`. Temperature is not settable for these agents, so repeated
runs are the only way to measure determinism.

## Metrics

### Success criterion: a blind judge, not text comparison

**Changed 2026-08-18.** Success was `normalized_match`: the agent's file, with comments
and whitespace stripped, equal to the maintainer's. The first two completed tasks, 40
runs, showed that this measures the wrong thing. In `crb_agentscope_342` the CI failure
is that Milvus Lite is unavailable on the runner:

```python
maintainer   if os.name == "nt": self.skipTest("Milvus Lite ... on Windows.")
agent        @unittest.skipUnless(_milvus_lite_available(), "... not installed")
```

Both resolve the failure and the agent's guard is the more general one, yet they share
no text and it scores zero. The maintainer's own patch in `crb_agno_180` also carries a
leftover `print("delta.tool_calls", ...)` debug line, so a run can only "succeed" there
by reproducing a mistake. `normalized_match` was 0.01 across all 480 runs and the
resulting memory effect of -0.004 was noise between two numbers pinned near zero by
construction.

Patch-region similarity was tried as a replacement and rejected on evidence: over the
same 40 runs it lands between 0.07 and 0.45 with no separation between correct and
incorrect repairs, because a valid rewrite scores no higher than a wrong edit. Any
threshold on it would be invented.

The criterion is therefore a **judge** (`src/ci_memory_agents/judge.py`,
`scripts/judge_runs.py`): given the failing log, the files the agent actually held, the
maintainer's patch as reference, and the agent's patch, does the agent's patch remove
the same root cause?

- **Blind to the condition.** The judge never sees `no_memory`/`with_memory`, the run
  index, the agent name or the memory block, so it cannot favour an arm.
- **Graded against the reference patch's root cause**, not against every error in the
  log. `repo_before` holds only the gold files, while the log reports a whole workflow;
  a judge asked "does the build pass now" would fail every run for not repairing code
  the agent was never shown.
- **Cheating counts as failure.** Deleting or emptying the failing test, removing an
  assertion, or skipping unconditionally is hidden, not repaired. `solved = fixes_failure
  and not cheats`. This matters because the prompt permits editing tests, and a
  conditional skip is often the maintainer's own fix.
- **Cached** as `judgement.json` per run, so re-scoring is free and every verdict is
  reproducible from the prompt hash recorded beside it.
- **Calibrated against a human.** `--mode calibration` draws a deterministic sample by
  hashing the run key, so the sample cannot be tuned after seeing results. Report
  judge-human agreement next to any judge-based number.
- `mechanism` records `same`/`different`/`none` against the reference patch. The
  `different` count is precisely what `normalized_match` scored as failure.

### Diagnostics, reported but not deciding success

- `exact_match`, `normalized_match` (whitespace/comment insensitive, as CI-Bench's
  `evaluate.sh`). Kept for comparability with prior work, and as the lower bound.
- `file_iou`, `file_precision`, `file_recall` (File IoU, from Learning to Commit).
  **These are near 1.0 by construction here**: `repo_before` contains only the gold
  files, 1 to 3 of them, so an agent that edits anything at all is localised. They
  measure the extract, not the agent, and must not be read as localisation ability.
- `line_deviation_ratio` (from Learning to Commit).
- `Pass@K`, unbiased estimator `1 - C(n-c,k)/C(n,k)`, computed over the judge verdict.

Scoring calibration of the textual diagnostics (`scripts/validate_pipeline.py`),
verified on all 24 tasks:

| baseline | exact | norm | IoU | recall | linedev |
|---|---|---|---|---|---|
| oracle | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 |
| noop | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |
| half | 0.58 | 0.58 | 0.77 | 0.77 | 0.00 |

## Analysis

Compare conditions on the same tasks, over the judge verdict. Report the memory effect
on Pass@K, stratified by `error_type`, since dependency and environment failures are
where project precedent should plausibly help and syntax errors are where it should not.

Only tasks completed under both conditions enter the comparison: a half-finished task
would otherwise contribute to one arm and nothing to the other, inventing an effect out
of scheduling order. Report `normalized_match` alongside as the strict lower bound, and
say plainly that it is not the criterion.

## Threats to validity

- Residual convention-level overlap in 3 of 24 tasks (above).
- Gold patches are whole commit diffs, so they include unrelated changes; this is why
  scoring is not byte-exact and why `file_iou` is reported alongside `exact_match`.
- **The judge is a model.** It replaces one flawed proxy with another: a fluent but
  wrong patch can read as a fix. It is mitigated by blinding, by the explicit cheating
  rule, and by human calibration on a fixed sample, but the agreement figure must be
  reported wherever the judge's numbers are. Judging with a different model than the one
  under test is the safer default.
- **Execution-based fail-to-pass verification is still not wired up.** It remains the
  strongest evidence and the judge is a stand-in for it, chosen because the extracts
  hold only the gold files and most of these projects will not install on the runner in
  the first place, which is what several of the tasks are failing over.
- **The log does not always describe the failure the gold patch fixes.** In 9 of 24
  tasks the compressed log never names a gold file. In `crb_agentscope_342` the log's
  headline error is an unrelated `mcp.client.streamable_http` ImportError, in a file the
  agent was not given, while the gold patch guards a Milvus Lite test. The agent is then
  asked to infer the intended repair from a mismatched problem statement. The judge
  compensates by grading against the reference patch's root cause; the task construction
  issue is not fixed by that and should be audited before scaling up.
- **The extract gives away localisation.** `repo_before` contains only the gold files,
  so the task is "what change" and never "which file". Any claim about localisation, and
  every File IoU number, has to be read with that in mind.
- 24 tasks is a pilot. Scale to the full 93 eligible instances before drawing
  conclusions.
