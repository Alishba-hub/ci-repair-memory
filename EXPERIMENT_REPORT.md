# Does an AI coding agent fix broken builds better if it remembers the project's past?

**An experimental report, written to be readable without knowing the codebase.**

Project: `ci-memory-agents` · Agent under test: Claude Code · Revised 5 September 2026
Status: **pilot in progress — 158 of 480 planned runs attempted (33%), 135 decided**

> ### Correction notice — 5 September 2026
>
> **Every rate in the 18 August version of this report was computed over the wrong
> population, and so was the "correction" that replaced it.** Both are withdrawn.
>
> The harness materialises a prompt and a pristine `workspace/` for every *planned* run,
> so a cell nobody has run yet is byte-identical to one where the agent declined to edit.
> The scorer could not tell them apart. Reading that one ambiguity two different ways
> produced two different papers:
>
> | | Denominator | no_memory | with_memory |
> |---|---|---|---|
> | 18 Aug report | 109 judged runs, 39 executed runs silently dropped | 70.4% | 72.7% |
> | `EVALUATION.md` "correction" | all 480 cells, **322 of which no agent ever ran** | 17.0% | 17.5% |
> | **Corrected** | **135 decided runs, of 158 attempted** | **57.4%** | **59.7%** |
>
> `oracle.run_attempted` now settles the question from `agent_meta.json`, which the
> runners write only after the agent process exits. Unattempted cells are missing data
> and are excluded from every rate, including under `--strict`. Section 7 below is
> regenerated from `scripts/score_runs.py --mode report`; nothing in it is transcribed
> by hand. Sections 2–6 were unaffected and stand as written.
>
> The consequences run further than the headline. See §7.2 for what survives, §7.8 for
> why the surviving difference is carried by tasks with one run each, and §7.9 for the
> sample-size calculation, which the same error understated by a factor of about twelve.
>
> ### Second notice: the verdict set is currently mixed, and §7's rates are provisional
>
> Re-judging is not finished. **90 of the 119 verdicts come from judge v2 and 29 from
> v3**, and the split is uneven across arms — no_memory has 18 at v3, with_memory 11.
> Those are different instruments: v3 adds a deterministic pre-screen and three-sample
> majority voting. A rate over a mixture is two measurements averaged, and an uneven
> split moves the *difference*, which is the entire result.
>
> How much it moves is not hypothetical. Re-judging 29 of the same 158 runs — no run
> changed, no code changed — moved the headline and flipped its sign:
>
> | Verdict set | no_memory | with_memory | difference |
> |---|---|---|---|
> | all v2 | 57.4% | 59.7% | +2.3 pp |
> | 90 v2 + 29 v3 (now) | 61.8% | 56.7% | **−5.1 pp** |
>
> A further 15 of 119 runs are flagged `unstable`: the samples disagreed within a single
> judging, so those verdicts would differ on another draw.
>
> **Every rate in §7 is therefore provisional and should not be quoted.** They are
> reproduced as the scorer currently computes them, and the numbers cited in the text
> are from the all-v2 set. The fix is to re-judge the whole set with one instrument
> (`judge_runs.py --force`, about 360 model calls), then regenerate §7. Until then,
> `score_runs.py --mode report` prints a "Judge consistency" block naming the split, so
> the mixture cannot be quoted by accident.
>
> This is not a setback for the report's argument — it is the argument. A judge whose
> verdicts move this much when re-sampled is exactly why §7.1's "no run has been decided
> by executing the workflow" is the finding that matters most.

---

## 1. The whole thing in one paragraph

Software projects run automated checks every time someone changes the code. When those
checks fail, the build is "broken" and somebody has to fix it. We are testing whether an
AI coding agent gets better at fixing a broken build when we also show it **how the same
project's earlier build failures were fixed**. We take real broken builds from real
open-source projects, hand them to the agent twice — once with that history, once without
— and count how often it produces a genuine repair. Across the 135 runs decided so far
the agent repairs **57.4% without history and 59.7% with it**. Treating each task's
repair *rate* as the observation, as the repeated-run design requires, the difference is
**+7.8 points with a 95% interval of [−4.8, +22.6]** — an interval that contains zero,
over 23 tasks, on a comparison that needs roughly 363 tasks to resolve an effect of the
size being claimed. **There is no detected effect.** What the pilot has established is
methodological, and §5, §6 and §7.8 are where that content is.

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

> **This section describes the pilot as it was actually run: two arms, 10 runs per
> condition, 24 tasks, one agent.** The design has since been widened -- four arms
> (`no_memory` and K = 1, 3, 5), 5 runs per condition, 30 tasks over 10 repositories,
> and three Copilot models -- and `src/ci_memory_agents/design.py` is the authority on
> it. Nothing below has been re-run under the wider design, so §3 is left describing
> the population the numbers in §7 actually came from. Rewriting it to match the
> current grid would attach these findings to a study that has not happened.
>
> The confound noted at the end of this section is one the wider design addresses: the
> placebo arm `foreign_memory` carries the same volume of history from a *different*
> project, and is laid out with
> `python scripts/run_experiment.py --mode prompts --conditions foreign_memory`.

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
In practice that block roughly doubles the prompt: measured over the runs actually
attempted, **9,154 characters without memory, 20,616 with** — a factor of 2.25.

**That doubling is a confound this two-arm design cannot resolve.** `with_memory`
differs from `no_memory` in two ways at once — whether the history is this project's,
and how much context there is — so any difference between them is attributable to
either. Separating them would need a third arm carrying the same volume of history
from a different project. That is left as future work, and any claimed effect must be
read with the confound stated.


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

The counts behind each of those, since they decide how far the design can scale:

| | Instances |
|---|---|
| In the dataset | 567 |
| With at least one strictly earlier failure in the same project | 464 |
| With **at least three** — the memory condition needs three | 347 |
| Semantic, i.e. not purely Code Linting (208) or Code Formatting (121) | 238 |
| After the ≤3-files and ≤2-per-project rules | **93 eligible** |
| Materialised so far, across 19 projects | **24** |

The 24: agentscope, agno, aider, aisuite, aws-cli, axolotl, calibre, conan, cpython, dask,
diffusers, django-import-export, docsgpt, httpx, kitty, litellm, openai-python, taipy,
uvicorn.

**93 is a hard ceiling on this dataset**, and §7.9 shows the study needs 363. That
arithmetic, not the budget, is what decides the shape of any submission.

### Why this dataset and not another

Four candidates were considered before CI-Repair-Bench was chosen.

| Dataset | What it offers | Why not primary |
|---|---|---|
| **CI-Repair-Bench** | Real CI failures with logs, patches, workflows, and an execution oracle | **Chosen.** Only one with all four |
| *When AI Agents Touch CI/CD Configurations* | Agent-generated PRs and workflow runs | Studies agent behaviour on CI changes, not repair of a given failure |
| *CI-Bench* | Real CI failures with logs, patches, repos, Docker environments | A viable backup; the Docker environments are an advantage if the execution oracle proves unworkable |
| *SWE-CI* | Repository history, commits, issues, CI environments | Supports the history-helps-agents premise; useful as related work rather than as the task source |

CI-Repair-Bench was picked because it is the only one that already contains failing
builds, the maintainers' fixes, *and* a defined way to check a repair by re-running the
workflow. CI-Bench remains the fallback if the execution oracle (§7.1) cannot be made to
work on forks.

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
| `Pass@K` | chance of success within K attempts, unbiased estimator `1 - C(n-c,k)/C(n,k)` computed over the judge verdict | consistency, not accuracy |

---

## 7. What we have found so far

> **Read this section as a pilot, and read §7.8 before quoting anything from it.** 158 of
> 480 planned runs have been attempted and 135 decided. Every figure below is regenerated
> by `python scripts/score_runs.py --agent claude-code --mode report` rather than
> transcribed, so re-running that command is the way to check this section.

### 7.1 Coverage — what has actually run

Coverage comes before rates, because the choice of denominator is what went wrong in the
previous two versions of this report. Each line here is a number someone might divide by.

| | Count | |
|---|---|---|
| Runs planned (24 tasks × 2 conditions × 10 runs) | 480 | the size of the plan |
| **Runs attempted** — an agent process ran and exited | **158** | 32.9% of the plan |
| Not attempted yet | 322 | **missing data, not failed repairs** |
| Attempted but killed by the timeout | 14 | a killed run is a partial answer |
| Attempted, ran, but no oracle reached a verdict | 9 | patch produced, never judged |
| **Runs decided** | **135** | the denominator for every rate below |

The 322 unattempted cells are the whole story of the correction notice. The harness
writes `prompt.md` and copies a pristine `workspace/` for every planned run up front, so
an unattempted cell on disk is byte-identical to one where an agent ran and declined to
edit anything. `oracle.run_attempted` now distinguishes them by the presence of
`agent_meta.json`, which the runners write only after the agent process exits.

Which oracle decided what, among attempted runs:

| Oracle | Count | Share |
|---|---|---|
| Execution — the workflow was re-run | **0** | **0%** |
| Static — deterministic, conclusive against the patch | 16 | 10% |
| Judge — a model | 119 | 75% |
| Undecided | 23 | 15% |

**Nothing here has been decided by executing anything.** `oracle_github.py`,
`oracle_local.py` and `validate_instances.py` are written and unrun; no `ci_outcome.json`
or `ci_baseline.json` exists anywhere under `runs/`. By the standard this project sets for
itself in `EVALUATION.md` §4.5 — *"an LLM judge with a published κ against a real oracle
is a defensible instrument; one without is an assertion"* — **every number below is
currently an assertion.** Cohen's κ is undefined because the comparison has never been
made. That is the single most important caveat in this document.

### 7.2 The headline

| | Without memory | With memory |
|---|---|---|
| Runs attempted | 79 | 79 |
| Runs decided | 68 | 67 |
| **Runs repaired** | **39 / 68 = 57.4%** | **40 / 67 = 59.7%** |
| Per-task repair rate, mean over 23 paired tasks | **56.1%** | **64.0%** |
| Tasks improved / worse / unchanged | — | 5 / 2 / 16 |

**Effect of memory: +7.8 percentage points on the per-task repair rate, 95% cluster
bootstrap interval [−4.8, +22.6].** The interval contains zero.

The per-task rate is the right unit and the run-level rate is not, for the reason
`stats.paired_rate_analysis` documents: ten runs of one task are not ten independent
observations, since they share the bug, the repository and the log. Tasks are the unit of
independence; repeated runs reduce the noise in each task's estimate rather than adding
observations. The run-level line is kept only because it is the number most easily
compared with other work.

**Is the effect real? No, and §7.8 explains why the point estimate is not even a stable
description of the data at hand.** Only 2 tasks differ on a solved-at-least-once basis,
both favouring memory; an exact McNemar test on 2 discordant pairs gives p = 0.5, and the
test cannot reach p < 0.05 below 6 discordant pairs. That is underpowered by
construction — the sample cannot detect an effect, which is not the same as showing there
is none.

### 7.3 The two repository scopes must not be pooled

23 tasks give the agent only the files the reference patch touches; one, `httpx 41`, has
been re-materialised as a full 122-file checkout. These are different tasks — under the
focused scope, fault localization is solved for free — and `EVALUATION.md` §6 says so
explicitly. The scorer now splits them rather than averaging over a mixture whose
proportions are an artefact of which tasks have been re-materialised so far.

| Scope | Condition | Decided | Repaired | Rate |
|---|---|---|---|---|
| focused | no_memory | 67 | 39 | 58.2% |
| focused | with_memory | 66 | 40 | 60.6% |
| full | no_memory | 1 | 0 | 0.0% |
| full | with_memory | 1 | 0 | 0.0% |

The `full` row is one run per arm and says nothing yet. It is shown because the headline
must not quietly absorb it.

### 7.4 Where runs succeed and fail

| Stage | Without memory | With memory |
|---|---|---|
| Attempted | 79 | 79 |
| Decided | 68 | 67 |
| Emitted no patch at all (deterministic, scored as a failure) | 10 | 6 |
| **Repaired the failure** | **39 (57%)** | **40 (60%)** |

The empty-patch rate among *attempted* runs is 16 / 158 = 10.1%, and 14 of those 16 are
the two `aisuite` tasks (see §7.7). The figure of about 70% quoted in `EVALUATION.md` §3
was the unattempted cells being counted as empty patches, and the diagnosis built on it —
that withholding files causes agents to decline — is not supported by the attempted runs.

### 7.5 How the repairs were written

Out of 119 judged runs:

| Judge verdict | Count |
|---|---|
| Repaired it **the same way** as the maintainer | 33 |
| Repaired it **a different way** | **63** |
| Did not address the root cause | 23 |
| Flagged as cheating (hiding the failure) | 12 |

**46 of the 79 successful repairs (58%) took a route the maintainer did not take.** Those
are exactly the runs the original text-matching criterion threw away as failures. This is
the strongest result the pilot has, it is independent of whether memory helps, and it is
what §6 exists to justify.

### 7.6 What memory costs

| | Without memory | With memory |
|---|---|---|
| Mean prompt size | 9,154 characters | **20,616 characters** |
| Median wall clock per run | 202 s | 174 s |
| Mean reply size | 1,906 characters | 1,784 characters |

Memory multiplies the prompt by 2.25 — and the runs finished *faster*, which is what you
would expect if the extra context shortens the agent's search. Both facts are why the
until it has been run.

### 7.7 Per-task results

Runs repaired per task, of runs decided. `0/0 of n` means n runs were attempted and none
could be decided; `not run` means the arm has no attempted run. Every task is listed,
including those contributing nothing — a table that omits a task for a reason correlated
with its outcome is a selection rule.

| Task | Scope | Without | With |
|---|---|---|---|
| agentscope 342 | focused | 7/10 | 8/10 |
| agno 180 | focused | 7/10 | 3/10 |
| aider 97 | focused | 9/10 | 10/10 |
| aisuite 372 | focused | 1/9 | 1/9 |
| aisuite 373 | focused | 0/4 | 0/3 |
| aws-cli 144 | focused | 2/2 | 2/2 |
| aws-cli 146 | focused | 2/2 | 2/2 |
| axolotl 459 | focused | 2/2 | 1/2 |
| axolotl 475 | focused | 1/2 | 2/2 |
| calibre 232 | focused | 1/2 | 1/2 |
| calibre 233 | focused | 1/1 | 1/1 |
| conan 542 | focused | 1/1 | 1/1 |
| conan 549 | focused | 1/1 | 2/2 |
| cpython 213 | focused | 1/2 | 1/2 |
| dask 300 | focused | 0/1 | 0/0 of 2 |
| diffusers 22 | focused | 0/1 | 1/1 |
| django-import-export 29 | focused | 1/1 | 1/1 |
| docsgpt 426 | focused | 0/1 | 0/1 |
| httpx 41 | **full** | 0/1 | 0/1 |
| kitty 313 | focused | 0/1 | 0/1 |
| litellm 396 | focused | 0/1 | 1/1 |
| openai-python 230 | focused | 1/1 | 1/1 |
| taipy 439 | focused | 1/1 | 1/1 |
| uvicorn 305 | focused | 0/1 | 0/1 |

`aisuite 373` appeared in the 18 August version as having "no scored runs at all". It has
20 attempted runs, 0 repaired in both arms. It was absent because its runs produced
nothing judgeable, not because they did not exist.

Both `aisuite` tasks should be dropped at instance validation: their reference patch is a
10-line `poetry.lock` content-hash update, which cannot be produced offline, and between
them they contribute 40 runs of noise. They are also where the judge is demonstrably
wrong — see §7.10.

`agno 180` is the one task where memory did clearly worse (7/10 → 3/10). It is also one of
the three tasks with residual memory overlap, one of the ten whose log names no file the
fix touches (§10.5), and the one whose reference patch contains the maintainer's leftover
debug `print()`. It is the most influential task in the run-level result and the most
broken one.

### 7.8 The difference is carried by the tasks with the fewest runs

This is the finding that should be read before any number in §7.2.

A task run once contributes a rate of 0 or 1 and moves the mean by a full 1/23 on a single
coin flip. A task run ten times contributes an estimate. `paired_rate_analysis` averages
them unweighted, so the thinnest evidence carries the most swing. Splitting the 23 paired
tasks by how much evidence each rests on:

| Subset | Paired tasks | Without | With | Difference |
|---|---|---|---|---|
| Fewer than 3 runs in an arm | 18 | 58.3% | 69.4% | **+11.1** |
| 3 or more runs in an arm | 5 | 48.2% | 44.2% | **−4.0** |

**The two halves disagree in sign.** Every task with enough repetitions to have a rate at
all points the other way. Task by task, only 8 of 24 move at all, and four of the six
improvements come from tasks with one or two runs per arm:

| Task | Runs per arm | Without | With | Δ |
|---|---|---|---|---|
| diffusers 22 | 1 | 0.00 | 1.00 | +1.00 |
| litellm 396 | 1 | 0.00 | 1.00 | +1.00 |
| axolotl 475 | 2 | 0.50 | 1.00 | +0.50 |
| conan 549 | 2 | 0.50 | 1.00 | +0.50 |
| axolotl 459 | 2 | 1.00 | 0.50 | −0.50 |
| agentscope 342 | 10 | 0.70 | 0.80 | +0.10 |
| aider 97 | 10 | 0.90 | 1.00 | +0.10 |
| agno 180 | 10 | 0.70 | 0.30 | −0.40 |

Run coverage is also badly unbalanced: five tasks hold 100 of the 158 attempted runs
(63%), and 16 of 24 tasks have exactly one run per arm. **The pooled +7.8 is a statement
about which tasks were scheduled first.** Equalising runs per task matters more here than
adding runs, and no pooled figure should be quoted until it is done. `score_runs.py
--mode report` now prints this split automatically so it cannot be overlooked again.

### 7.9 How many tasks this would actually need

The previous version of this calculation used a per-task standard deviation of 0.099 and
concluded that 31 tasks would suffice to detect 5 points. That SD was computed with the
322 unattempted cells included, which pin most tasks near 0 in both arms and collapse the
between-task variance the calculation depends on. Recomputed on the runs that were
actually attempted, the SD is **0.340**.

| Effect to detect | Tasks at 80% power, as previously published | Tasks at 80% power, corrected |
|---|---|---|
| 3 points | 88 | **1,007** |
| 5 points | 31 | **363** |
| 8 points | 13 | **142** |
| 10 points | 8 | **91** |
| The observed 7.8 points | — | **148** |

There are 93 eligible instances in the whole dataset. **An effect of the size this pilot
appears to show cannot be established at any scale this dataset supports**, and that
conclusion is available now rather than after another 322 runs. It is a finding, and it
should drive the submission decision instead of the 31-task figure it replaces.

### 7.10 The judge is wrong in ways we can already see

Two runs on `aisuite 372`, one in each arm, are graded as repairs and are almost certainly
not:

- `no_memory/run_07` — "solved, high confidence, same mechanism", for writing an
  **invented** `poetry.lock` content hash. The judge's own stated reason concedes it:
  *"though the hash value differs, its correctness depends on poetry's actual…"*
- `with_memory/run_01` — "solved", for **deleting `poetry.lock`** on the theory that
  Poetry would regenerate it.

Neither would turn a build green. This is exactly the "fluent but wrong patch reads as a
fix" failure mode §6 anticipates, caught in the wild, and its rate is unquantified because
no execution oracle has run. Two further caveats on the same instrument:

- All 119 cached verdicts carry `judge_version: 2`. The shipped `judge.py` is version 3,
  which adds the deterministic pre-screen and three-sample majority voting. **No result in
  this report was produced by the judge this repository ships**, and none of the stored
  verdicts records the sample agreement that v3 exists to measure.
- 9 attempted runs produced a patch that was never judged at all and are excluded as
  undecided rather than counted either way.

### 7.11 By failure type

Deliberately not reported. Every cell of that breakdown is smaller than the samples in
§7.8 that already disagree in sign with each other, and the previous version's table
invited exactly the reading it warned against. It returns when the runs are balanced.

---

## 8. What every part of the project is

### Top level

| Path | What it is |
|---|---|
Four documents, each with one job. If they ever disagree, the one nearest the code wins,
and the disagreement is a bug worth fixing rather than a matter of taste.

| Path | What it is |
|---|---|
| `README.md` | What this project is and how the repository is laid out. The front door |
| `HOW_TO_RUN.md` | How to actually run it. `python run.py` and what the flags mean |
| `EXPERIMENT_REPORT.md` | This file: the design, the findings, and what they cannot claim |
| `EVALUATION.md` | The oracle argument — how correctness is decided, and why. Written to be lifted into a paper's evaluation section |
| `run.py` | The single entry point. Everything else is a script it calls |
| `data/ci-repair-bench.parquet` | The raw 240 MB dataset download (gitignored; `scripts/import_ci_repair_bench.py` fetches it) |
| `tasks/` | The 24 built tasks, plus 3 hand-written demo fixtures for smoke-testing the harness |
| `runs/<agent>/<task>/<condition>/run_NN/` | One folder per run: prompt, agent reply, timings, token ledger, the judge's verdict, and a full copy of the workspace. **Gitignored** — results live on the machine that produced them |
| `results_archive/` | Frozen results from an earlier version of the protocol, kept for comparison only. Not current |
| `vscode-extension/` | A VS Code extension that drives GitHub Copilot, which has no command line |
| `START_HERE.bat` | Double-click launcher that runs `doctor` and says what to do next |

Two documents were removed in the September 2026 consolidation: `experiment_plan.md`,
whose design and threats content is §3–§5 and §10 here and whose numbers had gone stale;
and `research_meeting_reuse_notes.md`, whose dataset comparison is now §4.

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

The canonical run sequence now lives in [HOW_TO_RUN.md](HOW_TO_RUN.md), and the one-command
launcher is [RUN_EXPERIMENT.bat](RUN_EXPERIMENT.bat). Open the dashboard when you want a
browser view of progress and results.

---

## 10. What this cannot claim yet

Stated honestly, because a report that hides these is worth less than one that does not.
Items 1–11 are limits of the result. §10b lists things simply not written down yet, which
is a different kind of gap and should not be confused with this one.

1. **There is no detected effect, and the point estimate is not stable.** The 95% interval
   on the per-task difference contains zero, and the tasks with enough runs to have a rate
   at all point the *opposite* way from the tasks run once (§7.8). Nothing about memory
   should be claimed from this data in either direction.
2. **The effect being sought is probably not establishable with this dataset.** 363 paired
   tasks are needed to detect 5 points at 80% power; 93 instances are eligible in total
   (§7.9). This is the finding that should drive the submission decision.
3. **No execution-based verification has been performed — not once.** `oracle_github.py`,
   `oracle_local.py` and `validate_instances.py` are written and unrun; `ci_conclusion` is
   empty on all 480 verdict rows. No instance has been checked to be red before repair and
   green with the gold patch. Everything rests on the judge.
4. **The judge is a model with no measured agreement against anything.** Cohen's κ is
   undefined because the comparison requires an execution oracle that has not run. Two
   concrete false positives are already visible (§7.10), and all stored verdicts come from
   `judge_version: 2` rather than the v3 judge this repository ships.
5. **Prompt length is confounded with memory.** `with_memory` prompts are 2.25× longer.
   (§3) but has not been run, so "memory helps" and "more context helps" are currently the
   same measurement.
6. **Runs are badly unbalanced across tasks.** Five tasks hold 63% of attempted runs; 16 of
   24 tasks have one run per arm. Equalising this matters more than adding runs.
7. **Localisation is given away in 23 of 24 tasks.** `repo_before` contains only the files
   the fix touched, so the task is always "what change", never "which file". Every
   file-overlap number is near 1.0 by construction. The one full-repository task is
   reported separately (§7.3) and must not be pooled with the rest.
8. **The log does not always describe the failure the fix repairs.** In **10 of 24** tasks
   the compressed log never names a file the maintainer's patch touched. In
   `agentscope_342` the log's headline error is an unrelated import failure in a file the
   agent was never given. The judge compensates by grading against the reference patch's
   root cause, but the task-construction issue is real and should be fixed before scaling.
9. **Two tasks are unsolvable as constructed.** `aisuite 372` and `aisuite 373` require
   regenerating a `poetry.lock` content hash offline. They contribute 40 runs of noise and
   should be excluded at instance validation.
10. **Residual memory overlap in 3 of 24 tasks** (8–12%), all below the 25% threshold and
    all convention-level. A sensitivity run at different thresholds would settle it. The
    audit now also reports localisation leakage separately, which is currently zero and
    will not stay zero once the repositories are fully materialised.
11. **Only one agent, and one model.** Claude Code. `runs/copilot/` holds 480 materialised
    cells and **zero** attempted runs, so there is no cross-agent comparison yet — only
    scaffolding for one.

---

## 10b. Known gaps — things not written down yet

Distinct from §10. Those are limits of the finding; these are places where the repository
is simply missing something a reader or a successor would need. Listed so that the absence
is deliberate and visible rather than discovered later.

### Missing from the repository

| Gap | Why it matters | What to do |
|---|---|---|
| **No `LICENSE`** | Nothing states whether this can be reused, and the task folders contain code vendored from 19 open-source projects under their own licences. Without a statement the default is "all rights reserved", which blocks the artifact track of most venues | Add one, and a note that `tasks/*/repo_*` carry their upstream projects' licences, not this repository's |
| **No citation for CI-Repair-Bench** | The paper is referred to as `2604.27148v2` and the replication package as `github.com/RabeyaMuna/CI-REPAIR-BENCH`, but there is no full reference anywhere. A paper cannot ship like that | Add a `CITATION.md` with the full reference and the dataset URL |
| **No stated Python version** | The code uses `X | None` annotations (3.10+) and has been run only on 3.14. Nobody else knows what will work | State a floor and test against it |
| **`results_archive/` is undocumented** | One `.jsonl` from "an earlier version of the protocol". Which version, and why it was kept, is not recorded, so it cannot safely be compared with anything | Either document its provenance or delete it |
| **No record of which model the agent ran** | `agent_meta.json` stores the command and timings but not the model id. Runs from different model versions are indistinguishable after the fact, and `--agent-model` has to be *told* the answer for pricing | Record the model in `agent_meta.json` at run time |

### Measurements that do not exist yet

Each of these is a blank in this report, not a number that came out badly. They are
ordered by how much they would change what can be claimed.

| Missing measurement | Blocks | Cost |
|---|---|---|
| **Cohen's κ between the judge and real CI** | Every rate in §7. Without it they are inferences, by the standard `EVALUATION.md` §4.5 sets | ~50 CI runs; Actions minutes, free on public repos |
| **Instance validation** — red unpatched, green with the gold patch | Knowing the floor and ceiling of each task. An instance already green hands both arms a free success | 2 CI runs per instance, once, cached |
| **Human calibration of the judge** | `judge_runs.py --mode calibration` exists and draws a deterministic sample; no sample has been rated | A person, an afternoon |
| **A uniform re-judge** | §7 is currently 90 v2 verdicts and 29 v3 — see the second correction notice | ~360 judge calls |
| **Any Copilot run** | Every cross-agent claim. 480 cells are laid out and none attempted | 480 runs through the VS Code extension |
| **Per-failure-type breakdown** | Whether memory helps on dependency failures and not on syntax errors, which is the mechanism the hypothesis predicts | Falls out of a balanced run set; needs no new machinery |

### Design questions deliberately left open

- **Memory size and retrieval.** `select_tasks` always takes the three most recent prior
  failures. Whether the effect depends on how many, or on retrieving *similar* failures
  rather than recent ones, is untestable as built. Retrieval is the obvious comparison
  against the source benchmark's own approach, and parameterising it is cheap now and
  expensive after a full run.
- **Cursor and Aider.** Both are supported by `auto_run.py` presets and neither has been
  run. For Cursor specifically, log which model `auto` selected per run — otherwise model
  choice is an uncontrolled variable inside a single condition.
- **Non-Python projects.** Inherited from the benchmark. State it as a scope limit rather
  than treating it as a gap to fill.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **CI** | Continuous integration — the automated checks run on every code change |
| **Task** | One real broken build, with its code, log, and known fix |
| **Run** | One attempt by the agent at one task under one condition |
| **Memory** | Three earlier CI failures from the same project, with the patches that fixed them |
| **Attempted run** | A cell where an agent process actually ran, evidenced by `agent_meta.json`. The harness materialises a prompt and workspace for every *planned* run, so this is not the same as a cell existing on disk |
| **Decided run** | An attempted run that some oracle could grade — the denominator for every rate |
| **Gold / reference patch** | What the project's maintainers actually committed to fix it |
| **Leakage** | The answer being visible in the prompt, which would fake a positive result |
| **Judge** | A separate, condition-blind AI model that decides whether a repair is genuine |
| **Cheating** | Hiding a failure — deleting the test, removing an assertion, skipping unconditionally — rather than repairing it |
| **Pass@K** | The chance a task is solved within K attempts, `1 - C(n-c,k)/C(n,k)` for n runs of which c succeeded |
| **Paired task** | A task with finished runs in *both* conditions; only these can be compared |
| **Discordant pair** | A task solved in one condition but not the other — the only tasks carrying statistical signal |
| **McNemar test** | The correct significance test for paired yes/no outcomes |
| **Wilson interval** | A confidence interval that stays sensible at small samples and near-zero rates |

---

## 12. The short version, for someone with two minutes

We are testing whether showing an AI agent a project's past build failures helps it fix the
current one. We took 24 real broken builds from 19 open-source projects and ran an agent at
each one up to 10 times with the history and 10 times without, on identical prompts apart
from that history block.

Getting this to mean anything required three pieces of real work: compressing
125,000-character logs down to 3,000; proving the history does not secretly contain the
answer (it did, twice, until it was blocked); and replacing "did the agent write the
maintainer's code" with "did the agent actually fix the failure", judged blind — because 46
of 79 successful repairs were written a different way than the maintainer wrote them, and
the old measure called all 46 failures.

**The result on memory is: no detected effect.** Across 135 decided runs the agent repairs
57.4% of these builds on its own and 59.7% with the project's history; on the per-task rate
that is +7.8 points with a 95% interval of [−4.8, +22.6]. Worse for the hypothesis, the
apparent gain comes entirely from tasks that were run once or twice — every task with enough
repetitions to have a rate at all points the other way. And detecting an effect this size
would take roughly 148 paired tasks where 93 exist.

**Two earlier versions of this document reported otherwise, in opposite directions**, because
each divided by a different population: 71%/81% by dropping executed runs, 17.0%/17.5% by
counting 322 cells no agent ever ran. That the same 158 runs support all three numbers is a
better result than any of them, and it is what this pilot is actually worth publishing.
