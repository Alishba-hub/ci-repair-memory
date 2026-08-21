# Does an AI coding agent fix broken builds better if it remembers the project's past?

**An experimental report, written to be readable without knowing the codebase.**

Project: `ci-memory-agents` · Agent under test: Claude Code · Report generated 18 August 2026
Status: **pilot in progress — 109 of 480 planned runs finished (23%)**

---

## 1. The whole thing in one paragraph

Software projects run automated checks every time someone changes the code. When those
checks fail, the build is "broken" and somebody has to fix it. We are testing whether an
AI coding agent gets better at fixing a broken build when we also show it **how the same
project's earlier build failures were fixed**. We take real broken builds from real
open-source projects, hand them to the agent twice — once with that history, once without
— and count how often it produces a genuine repair. So far the agent repairs **71% of
tasks without history and 81% with it**, a difference of about 10 percentage points that
is *not yet statistically meaningful* because only 21 tasks have been completed both ways.

---

## 2. The question, in plain terms

### What is a "CI failure"?

CI stands for *continuous integration*. Every time a developer changes code, a server
automatically installs the project, runs its tests, and checks its style. If anything
breaks, CI produces a long error log and the change is blocked. Fixing that is a routine,
tedious, and very common job.

### What is "memory" here?

Not memory in the sense of a chatbot remembering your last message. Here it means:
**earlier CI failures from the same project, together with the patches that fixed them.**

The intuition is that projects fail in habitual ways. One project always breaks because a
dependency ships a bad version; another always breaks on Windows; another has a test that
needs a specific package pinned. A human maintainer knows these habits. An AI agent
arriving cold does not. We are testing whether handing it that history closes the gap.

### The hypothesis

> An agent repairs more CI failures, and more consistently, when it can see how the same
> project's earlier CI failures were diagnosed and fixed.

---

## 3. How the experiment is set up

Each **task** is one real broken build. The agent is given:

- the project's code as it was at the moment it broke,
- the CI error log explaining what went wrong,
- the workflow definition (the checks that were run).

It must edit the files so the build would pass. Then we compare two conditions:

| | Code at the broken commit | The failing log | Earlier failures + their fixes |
|---|---|---|---|
| **Without memory** (`no_memory`) | yes | yes | **no** |
| **With memory** (`with_memory`) | yes | yes | **yes — 3 earlier failures** |

**Important design point:** the failing log is given in *both* conditions. It is the
problem statement, not memory. Removing it from one arm would compare "fix this stated
bug" against "find the bug", which is a different question entirely.

The only difference between the two prompts is a block of text listing three earlier
failures from the same project, each with its error log and the patch that resolved it.
In practice that block roughly doubles the prompt: **10,685 characters without memory,
22,200 with**.

### The protocol

Every task is run **10 times in each condition**, each time against a fresh copy of the
broken code. These agents do not expose a temperature setting, so repeating runs is the
only way to see how consistent they are. That gives 24 tasks × 2 conditions × 10 runs =
**480 planned runs**.

---

## 4. Where the data comes from

**CI-Repair-Bench** — a public research dataset of 567 real CI failures from 103 Python
projects, with commits from October 2023 to November 2025. For each failure it stores the
broken commit, the fixed commit, the CI logs, and the patch the maintainers actually wrote.

Four properties of that dataset shaped the design, and each one cost real work to discover:

1. **There is no "passing" log.** Every one of the 567 stored logs is a failure log. You
   cannot show the agent what success looks like without re-running the build yourself.
2. **The logs are far too big to paste into a prompt.** Median 125,000 characters; the
   largest is 52 million. They had to be compressed (see §5.1).
3. **Projects must be grouped by project name, not by owner.** The dataset's rows come
   from benchmark-owned forks, so `agno` appears under three different owners. Grouping by
   owner would split one project's history into three and inflate the project count from
   103 to 139.
4. **The logs were regenerated, not archived.** Log timestamps are 2026 while the commits
   are 2023–2025, so the builds were re-run on forks later. They must not be described in
   a paper as the original historical builds.

### Which failures we selected

From 567 instances we kept only those that make the comparison meaningful:

- the project must have **at least 3 strictly earlier failures** (otherwise there is no
  memory to give);
- the failure must be **semantic**, not just formatting or linting — those are 58% of the
  dataset and their "fix" is often a whitespace change, which measures a code formatter
  rather than repair ability;
- the maintainer's patch must touch **at most 3 files**;
- **at most 2 tasks per project**, round-robin — `agno` alone contributes 84 of the 567
  instances, and taking them in order would have made this a study of `agno`.

That leaves 93 eligible instances. **24 are currently built out, across 19 projects**:
agentscope, agno, aider, aisuite, aws-cli, axolotl, calibre, conan, cpython, dask,
diffusers, django-import-export, docsgpt, httpx, kitty, litellm, openai-python, taipy,
uvicorn.

---

## 5. The three hard problems, and how they were solved

This is the part that distinguishes the project from "prompt an AI and count wins".

### 5.1 The logs are enormous

A median CI log is 125,000 characters — roughly 31,000 tokens, before you add the code.
`log_compressor.py` cuts them by about 33× to a 3,000-character budget by keeping only the
lines around actual failure markers (`##[error]`, `Traceback`, `FAILED`, `ModuleNotFoundError`
and similar) plus the tail of the log, and discarding runner boilerplate. Checked on 81
sampled instances, an explicit failure signal survived in **81 out of 81**.

### 5.2 Memory can secretly contain the answer

This is the most important methodological finding in the project, and it is a result worth
reporting on its own.

**Being chronologically earlier is not enough to be safe.** Two ways the answer leaked:

- **Recurring patches.** In `taipy`, an earlier instance predates the target by nine days
  yet already contains **97% of the target's fix**. Chronology admits it; content must
  exclude it. Had it stayed in, the memory condition would have been handed the answer and
  the experiment would have "proved" its own hypothesis.
- **Context lines in diffs.** In `axolotl`, a line that the target's fix *adds* appears as
  an unchanged *context* line inside an earlier patch. A check that only looks at added
  lines reports 0% overlap while the agent can still read the answer.

The rule now: a memory item is rejected if **more than 25% of the target's added lines
appear anywhere in that item's text** — patch *and* compressed log.

**Current audit result** (`scripts/audit_leakage.py`, all 24 tasks):

| | Result |
|---|---|
| Tasks where `no_memory` leaks anything | **0 of 24** — clean by construction |
| Tasks with residual overlap in `with_memory` | 3 of 24 |
| Worst residual | `conan_549` 12%, `taipy_439` 9%, `agno_180` 8% |
| Any task over the 25% threshold | none |

Those residuals are project conventions such as a recurring `setuptools = "<81"` pin —
arguably exactly the signal the memory condition is meant to supply. Where the line falls
between "useful precedent" and "answer key" is a judgement call, and it should be stated
as a threat to validity and settled with a sensitivity run at different thresholds.

### 5.3 Measuring "did it fix it" is genuinely hard

This is the single biggest thing to understand about the results, so it gets its own
section.

---

## 6. How success is measured — and why the first attempt was wrong

### The obvious approach, and why it failed

The original criterion was **text comparison**: strip comments and whitespace, then check
whether the agent's file equals the maintainer's file. It is objective, cheap, and
reproducible. It is also, on this data, close to useless.

A worked example from `crb_agentscope_342`. The build fails because a database component
(Milvus Lite) is not available on the test machine.

```python
# What the maintainer wrote
if os.name == "nt":
    self.skipTest("Milvus Lite ... on Windows.")

# What the agent wrote
@unittest.skipUnless(_milvus_lite_available(), "... not installed")
```

Both resolve the failure. The agent's guard is arguably the *better* one — it checks
whether the component is actually there, rather than assuming Windows is the only place it
is missing. They share no text, so text comparison scores the agent **zero**.

It gets worse. The maintainer's own patch in `crb_agno_180` contains a leftover
`print("delta.tool_calls", ...)` debug line. Under text comparison, an agent could only
"succeed" on that task by **reproducing the maintainer's mistake**.

Across all runs, text matching scored about **1%**, and the resulting memory effect was
−0.004 — noise between two numbers pinned near zero by the metric's own construction.

A softer version, patch-region similarity, was tried and **rejected on evidence**: over 40
runs it landed between 0.07 and 0.45 with no separation at all between correct and
incorrect repairs. Any threshold on it would have been invented rather than justified.

### The criterion actually used: a blind judge

Success is now decided by a **separate AI model acting as a judge** (`judge.py`,
`scripts/judge_runs.py`). It sees the failing log, the files the agent was allowed to
touch, the maintainer's patch as a reference, and the agent's patch. It answers one
question: **does this change remove the same root cause the maintainer's patch removed?**

Five design decisions make that defensible:

| Decision | Why |
|---|---|
| **Blind to the condition** | The judge never sees `no_memory` / `with_memory`, the run number, the agent name, or the memory block, so it cannot favour one arm. |
| **Graded against the reference patch's root cause** | The code extract holds only the files the fix touched, while the log reports a whole workflow. A judge asked "does the build pass now?" would fail every run for not repairing code the agent was never given. |
| **Cheating counts as failure** | Deleting the failing test, removing an assertion, or skipping unconditionally hides the failure rather than repairing it. `solved = fixes_failure AND NOT cheats`. |
| **Cached and reproducible** | Every verdict is written to `judgement.json` beside the run, with the hash of the prompt that produced it. Re-scoring the whole experiment is free. |
| **Calibrated against a human** | `--mode calibration` picks a sample by hashing the run key, so the sample cannot be cherry-picked after seeing results. |

**This is a real limitation, stated plainly:** the judge is a model, so it replaces one
imperfect proxy with another. A fluent but wrong patch can read as a fix. The mitigations
are blinding, the explicit cheating rule, and human calibration — and the human-agreement
figure must be reported anywhere the judge's numbers appear.

### The measures reported but *not* deciding success

| Measure | What it means | Why it does not decide |
|---|---|---|
| `exact_match` | byte-identical to the maintainer's file | far too strict; see above |
| `normalized_match` | identical ignoring comments/whitespace | the old criterion; kept as a strict lower bound and for comparability with prior work |
| `file_recall` / `precision` / `IoU` | did it edit the right files | **near 1.0 by construction** — the extract contains only the files the fix touched, so anything that edits at all is "localised". These measure the extract, not the agent. |
| `line_deviation_ratio` | how much bigger the change was than the maintainer's | a size diagnostic, not a correctness one |
| `Pass@K` | chance of success within K attempts | consistency, not accuracy |

---

## 7. What we have found so far

> **Read this section as a pilot.** 109 of 480 runs are done and only 21 tasks have
> been completed in both conditions. Every number below can still move.

### 7.1 Coverage — what has actually run

| | Count |
|---|---|
| Runs planned | 480 (24 tasks × 2 conditions × 10 runs) |
| **Runs scored** | **109** |
| Cut off by the timeout (excluded — a killed run is a partial answer, not a wrong one) | 14 |
| Finished but changed no files (excluded — nothing to score) | 3 |
| Not started yet | 354 |
| Judged | 109 of 109 |
| Total agent time consumed | about 5 hours |

### 7.2 The headline

| | Without memory | With memory |
|---|---|---|
| **Tasks repaired** (solved in at least one run, 21 paired tasks) | 15/21 = **71.4%** | 17/21 = **81.0%** |
| 95% confidence interval | [50.0%, 86.2%] | [60.0%, 92.3%] |
| **Runs repaired** | 38/54 = 70.4% | 40/55 = 72.7% |
| **Pass@1** (single attempt, 21 tasks) | 63.3% | 71.9% |
| Pass@2 — *only 7 tasks have 2 runs in both conditions* | 98.1% | 93.0% |

Pass@3 and beyond are deliberately **not shown**: fewer than 5 tasks have that many runs in
both conditions, and a curve drawn over 2 tasks would describe which tasks happened to
finish first rather than anything about the agent.

**Effect of memory: +9.5 percentage points on task solve rate**, bootstrap 95% interval
[0.0, +23.8] points.

**Is it real? Not yet demonstrable.** Only **2 tasks** differ between the conditions (both
in memory's favour; none went the other way). An exact McNemar test on 2 disagreements
gives **p = 0.5**. The test mathematically *cannot* reach p < 0.05 with fewer than 6
disagreements, so this is **underpowered by construction** — the sample is too small to
detect an effect, which is not the same as showing there is none.

The direction is encouraging and entirely consistent with the hypothesis. It is not yet
evidence.

### 7.3 Where runs succeed and fail

| Stage | Without memory | With memory |
|---|---|---|
| Scored runs | 54 | 55 |
| Edited at least one file | 54 (100%) | 55 (100%) |
| Touched a file the real fix touched | 54 (100%) | 55 (100%) |
| Touched *every* file the real fix touched | 49 (91%) | 50 (91%) |
| **Repaired the failure (judge)** | **38 (70%)** | **40 (73%)** |
| Also matched the maintainer's text | 4 (7%) | 3 (5%) |
| Byte-identical to the maintainer | 2 (4%) | 2 (4%) |

The gap between rows 5 and 6 is the entire argument of §6: **the agent repairs the failure
about 70% of the time and reproduces the maintainer's text about 6% of the time.**

### 7.4 How the repairs were written

Out of 109 judged runs:

| Judge verdict | Count |
|---|---|
| Repaired it **the same way** as the maintainer | 32 |
| Repaired it **a different way** | **62** |
| Did not address the root cause | 15 |
| Flagged as cheating (hiding the failure) | 5 — 4 without memory, 1 with |
| Judge confidence high / medium / low | 47 / 58 / 4 |

**62 of 109 runs repaired the failure by a route the maintainer did not take.** Those 62
runs are exactly what the old text-matching criterion threw away as failures. That single
number is the strongest justification for the change of criterion, and it is a reportable
result in its own right.

### 7.5 By failure type

Project history should plausibly help on dependency and environment failures and not on
syntax errors, so a pooled number can hide opposite effects.

| Failure type | Repaired without | Repaired with | Runs |
|---|---|---|---|
| Dependency Issues | 18/29 (62%) | 17/29 (59%) | 58 |
| Syntax Error | 17/21 (81%) | 14/21 (67%) | 42 |
| Test Failure | 13/19 (68%) | 15/19 (79%) | 38 |
| Package Installation Error | 11/13 (85%) | 12/13 (92%) | 26 |

Memory looks better on test and installation failures and worse on syntax errors, which is
the direction the hypothesis predicts — but every cell here is small enough that a couple
of runs would flip it. Do not report these as findings yet.

### 7.6 What memory costs

| | Without memory | With memory |
|---|---|---|
| Mean prompt size | 10,685 characters | **22,200 characters** |
| Mean wall clock per run | 179 s | 156 s |
| Median wall clock | 169 s | 146 s |
| Mean reply size | 1,738 characters | 1,692 characters |

Memory roughly **doubles the prompt** — and, interestingly, the runs finished *faster*
rather than slower, which is what you would expect if the extra context shortens the
agent's search. Any claimed benefit has to be read against that doubled prompt cost, or
memory looks free when it is not.

### 7.7 Per-task results

Runs repaired, per task. `0/0` means that condition has no finished runs yet. The 24th task,
`aisuite 373`, has no scored runs at all and so does not appear.

| Task | Failure types | Without | With |
|---|---|---|---|
| agentscope 342 | Dependency, Test Failure | 7/10 | 8/10 |
| agno 180 | Syntax, Dependency | 7/10 | 3/10 |
| aider 97 | Assertion, Package Install | 9/10 | 10/10 |
| aisuite 372 | Package Install | 0/0 | 1/1 |
| aws-cli 144 | Test Failure | 2/2 | 2/2 |
| aws-cli 146 | Runtime, Test Failure | 2/2 | 2/2 |
| axolotl 459 | Package Install | 2/2 | 1/1 |
| axolotl 475 | Dependency | 1/1 | 2/2 |
| calibre 232 | Dependency | 1/2 | 1/2 |
| calibre 233 | Dependency | 1/1 | 1/1 |
| conan 542 | Syntax | 1/1 | 1/1 |
| conan 549 | Test Failure, Runtime | 1/1 | 2/2 |
| cpython 213 | Runtime | 1/2 | 1/2 |
| dask 300 | Test, Runtime, Dependency | 0/1 | 0/0 |
| diffusers 22 | Dependency, Runtime | 0/1 | 1/1 |
| django-import-export 29 | Code Formatting, Test Failure | 1/1 | 1/1 |
| docsgpt 426 | Package Install, Dependency | 0/1 | 0/1 |
| httpx 41 | Test Failure | 0/1 | 0/1 |
| kitty 313 | Dependency, Runtime | 0/1 | 0/1 |
| litellm 396 | Assertion, Runtime | 0/1 | 1/1 |
| openai-python 230 | Assertion, Environment | 1/1 | 1/1 |
| taipy 439 | Dependency | 1/1 | 1/1 |
| uvicorn 305 | Test, Environment | 0/1 | 0/1 |

`agno 180` is the one task where memory did clearly worse (7/10 → 3/10) and is worth
reading run by run in the dashboard. It is also one of the three tasks with residual
memory overlap, and its reference patch is the one containing the maintainer's leftover
debug line.

---

## 8. What every part of the project is

### Top level

| Path | What it is |
|---|---|
| `HOW_TO_RUN.md` | The short operational guide — start here to actually run something |
| `experiment_plan.md` | The rigorous version of this report: design decisions, evidence, threats to validity |
| `README.md` | Repository overview and the command-line pipeline |
| `EXPERIMENT_REPORT.md` | This file |
| `research_meeting_reuse_notes.md` | Which published datasets were considered and why CI-Repair-Bench was chosen |
| `data/ci-repair-bench.parquet` | The raw 240 MB dataset download |
| `tasks/` | The 24 built tasks (plus 3 hand-written demo fixtures) |
| `runs/<agent>/<task>/<condition>/run_NN/` | One folder per run: prompt, agent reply, timings, the judge's verdict, and a full copy of the workspace |
| `results_archive/` | Frozen results from an earlier version of the protocol |
| `vscode-extension/` | A VS Code extension that drives GitHub Copilot, which has no command line |
| `START_HERE.bat` | Double-click launcher for the dashboard |

### What one task folder contains

```
tasks/crb_<project>_<instance>/
  repo_before/                    the code as it was when the build broke
  repo_after/                     the code after the maintainers fixed it
  gold_patch.diff                 the maintainers' actual patch
  ci_logs/failed.compressed.log   the failing log, compressed to ~3,000 characters
  memory/                         3 earlier failures: prior_NN_<id>.log and .diff
  workflow.yml                    the CI checks that were run
  metadata.json                   IDs, dates, error types, which files the fix touched
```

### What one run folder contains

```
runs/claude-code/<task>/<condition>/run_01/
  prompt.md            exactly what the agent was asked
  workspace/           a fresh copy of repo_before that the agent edits in place
  agent_response.md    what the agent said
  agent_meta.json      exit code, duration, prompt and reply sizes, timestamp
  judgement.json       the judge's verdict, with the hash of the judging prompt
  agent_timeout.txt    present only if the run was killed for running too long
```

### The library (`src/ci_memory_agents/`)

| Module | Responsibility |
|---|---|
| `importer.py` | Turns dataset rows into task folders on disk |
| `memory.py` | Task selection and **leakage control** — the rules from §5.2 live here |
| `log_compressor.py` | Shrinks 125,000-character CI logs to a usable 3,000 |
| `loader.py` | Reads task folders back into Python objects |
| `prompt_builder.py` | Builds the two prompts; the *only* difference between them is the memory block |
| `judge.py` | The blind judge: prompt, verdict parsing, caching |
| `evaluator.py` | The textual diagnostics — file overlap, line deviation, per-file checks |
| `stats.py` | Wilson intervals, exact McNemar, paired bootstrap |
| `dashboard_state.py` | Scores every run and assembles the summary the UI draws |
| `report.py` | Builds the standalone HTML report |
| `ui.html` | The dashboard itself — charts, per-task drill-down, per-run inspector |

### The scripts (`scripts/`)

| Script | What it does |
|---|---|
| `import_ci_repair_bench.py` | Builds task folders from the dataset |
| `audit_leakage.py` | **Proves the experiment is fair** — reports how much of each answer is visible in each condition's prompt |
| `validate_pipeline.py` | **Proves the scorer works** — a perfect submission must score 1.00, a do-nothing submission 0.00, a half-fix in between |
| `run_experiment.py` | Generates prompts and workspaces; also scores from the terminal |
| `auto_run.py` | Runs the agent over every pending run, in parallel, resumable |
| `judge_runs.py` | Runs the judge over finished runs; also `--mode report` and `--mode calibration` |
| `dashboard.py` | Serves the web dashboard |
| `make_report.py` | Writes the standalone HTML report to a file |

### Two safety properties worth knowing

- **Everything resumes.** Both runners skip any run that already has an `agent_meta.json`,
  and neither will ever overwrite a workspace someone edited by hand. An interrupted batch
  is restarted by re-issuing the same command.
- **Nothing is silently dropped.** Timed-out runs and runs that changed no files are
  excluded from scoring — because a killed run is a partial answer, not a wrong one — but
  they are counted and displayed, so the coverage numbers always add up to the plan.

---

## 9. How to run it

```powershell
cd D:\research\ci-memory-agents

# 1. Watch progress and results in a browser
python scripts\dashboard.py --port 8004 --agent claude-code

# 2. Run the agent over pending runs (resumable, parallel)
python scripts\auto_run.py --preset claude --agent claude-code `
  --exe "<path to claude.exe>" --limit 40 --parallel 6 --timeout 900

# 3. Judge the finished runs
python scripts\judge_runs.py --agent claude-code --exe "<path to claude.exe>" --parallel 6

# 4. Health checks
python scripts\audit_leakage.py
python scripts\validate_pipeline.py
```

Open <http://localhost:8004> and click **Results**. It gives coverage, an expandable row
per task, every individual run, and — behind the **View** button — the exact prompt that
run was given, what the agent replied, the change it made, and the maintainer's fix side
by side.

One practical warning: **turn off sleep before a long batch.** A sleeping machine kills
runs mid-edit, and those are recorded as timeouts and excluded, so the work is wasted.

---

## 10. What this cannot claim yet

Stated honestly, because a report that hides these is worth less than one that does not.

1. **The sample is too small.** 21 paired tasks with 2 disagreements cannot reach
   statistical significance whatever the direction. The full 93 eligible instances are the
   target before any conclusion is drawn.
2. **The judge is a model.** It is blinded, has an explicit anti-cheating rule, and caches
   reproducible verdicts — but human calibration is set up and **not yet reported**. Until
   it is, every judge-based number carries an unquantified error bar.
3. **No execution-based verification.** The strongest possible evidence is running the
   build and watching it go from fail to pass. That is not wired up, mostly because the
   extracts hold only a few files and most of these projects will not install on the test
   machine — which is itself what several of the tasks are failing over. The judge is a
   deliberate stand-in.
4. **Localisation is given away.** `repo_before` contains only the files the fix touched,
   so the task is always "what change", never "which file". Every file-overlap number is
   near 1.0 by construction and must not be read as evidence that the agent is good at
   finding the right file.
5. **The log does not always describe the failure the fix repairs.** In 9 of 24 tasks the
   compressed log never names a file the maintainer's patch touched. In `agentscope_342`
   the log's headline error is an unrelated import failure in a file the agent was never
   given. The judge compensates by grading against the reference patch's root cause, but
   the underlying task-construction issue is real and should be audited before scaling up.
6. **Residual memory overlap in 3 of 24 tasks** (8–12%), all below the 25% threshold and
   all convention-level. A sensitivity run at different thresholds would settle it.
7. **Only one agent so far.** Claude Code. Copilot is wired up through the VS Code
   extension; Cursor and Aider are supported but not run. With a single agent, any result
   is about that agent, not about AI agents in general.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **CI** | Continuous integration — the automated checks run on every code change |
| **Task** | One real broken build, with its code, log, and known fix |
| **Run** | One attempt by the agent at one task under one condition |
| **Condition** | `no_memory` or `with_memory` — the only thing being varied |
| **Memory** | Three earlier CI failures from the same project, with the patches that fixed them |
| **Gold / reference patch** | What the project's maintainers actually committed to fix it |
| **Leakage** | The answer being visible in the prompt, which would fake a positive result |
| **Judge** | A separate, condition-blind AI model that decides whether a repair is genuine |
| **Cheating** | Hiding a failure — deleting the test, removing an assertion, skipping unconditionally — rather than repairing it |
| **Pass@K** | The chance a task is solved within K attempts |
| **Paired task** | A task with finished runs in *both* conditions; only these can be compared |
| **Discordant pair** | A task solved in one condition but not the other — the only tasks carrying statistical signal |
| **McNemar test** | The correct significance test for paired yes/no outcomes |
| **Wilson interval** | A confidence interval that stays sensible at small samples and near-zero rates |

---

## 12. The short version, for someone with two minutes

We are testing whether showing an AI agent a project's past build failures helps it fix the
current one. We took 24 real broken builds from 19 open-source projects and ran an agent at
each one 10 times with the history and 10 times without, on identical prompts apart from
that history block.

Getting this to mean anything required three pieces of real work: compressing 125,000-character
logs down to 3,000; proving the history does not secretly contain the answer (it did, twice,
until it was blocked); and replacing "did the agent write the maintainer's code" with "did the
agent actually fix the failure", judged blind — because 62 of 109 correct repairs were written
a different way than the maintainer wrote them, and the old measure called all 62 failures.

So far, with about a quarter of the runs done: **the agent fixes 71% of these builds on its own
and 81% with the project's history.** The direction supports the hypothesis. The sample is too
small to prove it, and saying so is part of the result.
