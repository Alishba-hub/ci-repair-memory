# Evaluation methodology: what CI-Repair-Bench does, what we did, what changed

This is the answer to the question raised in the 28 August meeting — *how do you decide
if a result is a pass?* — and the record of what was changed in response. It is written
to be lifted into the paper's evaluation section.

The design, the findings and the full limitations list live in `EXPERIMENT_REPORT.md`;
this file covers only how correctness is decided and why.

Sources: the CI-Repair-Bench paper (`2604.27148v2`) **and** its replication package
(`github.com/RabeyaMuna/CI-REPAIR-BENCH`), which do not agree with each other. Where they
differ, the code is what produced their published numbers, and it is what we match.

---

## 1. Does CI-Repair-Bench ship a test suite?

**No — and it does not use one.** The HuggingFace parquet has 17 columns:

```
language, id, repo_owner, repo_name, head_branch, workflow_name, workflow_filename,
workflow_path, sha_fail, sha_success, workflow, logs, diff, changed_files,
commit_link, error_type, commit_date
```

There is no test harness, no Docker image, no `FAIL_TO_PASS`/`PASS_TO_PASS` list, no
per-instance test command. This is deliberate and is the benchmark's central claim
(§1, Table 1): SWE-bench-family benchmarks use **test pass/fail** as the oracle;
CI-Repair-Bench rejects that as too narrow, because a CI failure is often a formatting,
linting, dependency, or environment failure that no unit test would catch.

> "Repair correctness is evaluated via **full GitHub Actions re-execution** rather than
> test-centric oracles, enforcing all CI validation stages, including formatting,
> linting, configuration checks, environment setup, and test execution under native CI
> settings." (§1)

So the action item "investigate whether CI-Repair-Bench includes existing test suites; if
not, develop our own" resolves the other way: **we should not write test suites.** Doing
so would abandon the property that makes this dataset worth using and would make our
numbers incomparable to theirs. The oracle is the workflow.

## 2. Their oracle, exactly

From `benchmark_functions.py` and `benchmark.py`, this is the whole loop:

| Step | Function | What it does |
|---|---|---|
| 1 | `get_repo` | Clone the **fork** at `benchmark_owner/repo`, `fetch origin <sha_fail>`, `reset --hard`, `clean -fdx`, branch `{user}__{model}__id_{id}` |
| 2 | `copy_and_edit_workflow_file` | Write the instance's `workflow` to `.github/workflows/<basename>`, set `on: push`, **delete every other workflow** except reusable ones it references |
| 3 | `fix_apply_generated_patch` | `git apply --check --3way` as a gate, then `git apply --3way`. Patches failing the gate are skipped |
| 4 | `push_repo` | `git add .`, commit, `git push --force` → GitHub Actions fires |
| 5 | `get_run_data` | Poll `GET /repos/{owner}/{repo}/commits/{sha}/check-runs` every 15 min, up to 25 cycles |
| 6 | — | **success** iff every conclusion is `success`; **failure** if any is `failure`; `waiting` if incomplete; else `error` |

Metric: **Pass@1** = |instances whose CI went green| / |567|. Instances whose CI could
not be triggered count as **failures**. They also report **Applied Patches** (how many
candidates passed the `git apply --check` gate) and per-error-type accuracy.

Their reported results — the band our numbers should land in:

| Model | Applied patches | Pass@1 |
|---|---|---|
| GPT-5-mini | 455 / 567 | **18.9%** |
| DeepSeek-Coder | 438 | 15.9% |
| DeepSeek-Chat | 416 | 13.2% |
| GPT-4o-mini | 420 | 7.9% |

Localization is scored separately: Top-1 41.6–45.3%, MAP 37.9–42.5%.

**The oracle costs no model tokens.** It costs GitHub Actions minutes, which are free on
public repositories. This inverts the cost model discussed in the meeting: the expensive
part is generating patches, not judging them. Replacing execution with an LLM judge
bought nothing and cost reliability.

### Where their paper and their code disagree

Worth a sentence in our related-work section, and it affects what we should copy:

| Claimed in §3.1 | Present in the code? |
|---|---|
| Matrix dimensions collapsed | **No** — the matrix runs in full |
| Non-validation steps (publish/upload) stripped | **No** — every step is kept |
| Gold patch re-run to confirm the standardized workflow preserves the original outcome; non-matching instances excluded | **No** — never executed |

The third omission is the significant one: nothing in their pipeline verifies that an
instance is red before a repair is attempted. We implement it (§4 below).

## 3. What our pipeline did, and why its numbers were wrong

Two defects, both fatal, both now fixed. The full account, with the three-way denominator
table, is the correction notice at the top of `EXPERIMENT_REPORT.md`; only what bears on
the *oracle* is repeated here.

**(a) The denominator.** `judge_runs._judgeable` skipped any run whose workspace was
unchanged, dropping those runs from judging and from every rate downstream. The first fix
for it — score every run directory instead — was wrong in the other direction and worse:
the harness materialises a prompt and a pristine workspace for every *planned* run, so of
the 480 cells on disk only 158 have an `agent_meta.json`, and the other 322 were never run
by anything. Scoring them as empty patches counted 322 pieces of missing data as 322
failed repairs.

```
original (judged runs only)     no_memory 0.616   with_memory 0.693   n=119
first "correction" (all cells)  no_memory 0.170   with_memory 0.175   n=480  <- 322 never ran
corrected (decided runs)        no_memory 0.574   with_memory 0.597   n=135
```

An agent that emits nothing has failed to repair the build, and CI-Repair-Bench is
explicit that non-applicable patches count as failures. But a cell in which no agent was
ever invoked is not an agent emitting nothing: their convention covers repair attempts
that could not be adjudicated, not cells never dealt an attempt. `oracle.run_attempted`
(§4.0) draws that line.

**The 17.0%/17.5% figure is withdrawn**, along with the claim that it "sits inside their
band" — the agreement with their 7.9–18.9% Pass@1 was manufactured by the 322 phantom
failures. On a like-for-like denominator this harness reports 57%/60%, *higher* than their
band, and the reason is (b).

**(b) The agent is handed the answer.** `importer.build_task` fetched only the files the
gold patch touches and called that `repo_before`. Fault localization — which their paper
measures as a task in its own right, and at which the best model scores 45% Top-1 — is
solved for free in 23 of our 24 tasks. That is the live explanation for a repair rate
three times theirs, and `scripts/materialize_repos.py` is the fix. Rates from the two
scopes cannot be pooled; `score_runs.py --mode report` splits them rather than averaging.

The empty-patch diagnosis built on (a) does not survive it. The claimed ~70% empty-patch
rate was the unattempted cells being counted as empty patches. **Among runs actually
attempted it is 16 / 158 = 10.1%**, and 14 of those 16 are the two `aisuite` instances,
whose reference patch is a `poetry.lock` content hash that cannot be regenerated offline.
Agents do sometimes decline for want of a file — *"I couldn't read `httpx/_config.py`"* —
but it is a rare event, not the dominant failure mode. Keep the `materialize_repos` work,
which is right for reason (b) regardless.

## 4. What the pipeline does now

Precedence: **deterministic → execution → model**, with the deciding oracle recorded on
every verdict (`src/ci_memory_agents/oracle.py`).

### 4.0 `oracle.run_attempted` — before any check runs at all
Whether an agent ever ran in this cell, read from `agent_meta.json`. This has to be the
first question, because no patch-level check can distinguish an unattempted cell from an
attempted one that changed nothing: both hold a pristine copy of `repo_before`. Getting
this wrong is what produced the withdrawn 17.0%/17.5% in §3. Unattempted cells are missing
data and are excluded from every rate, including under `--strict`; `--mode report` prints
the coverage ladder — planned, attempted, timed out, decided — so the denominator a rate
was taken over is always visible rather than assumed.

### 4.1 `static_checks.py` — exact, no model
AST-level checks that settle a run without a model call: empty patch, file that no longer
parses, deleted test file, removed test function, removed assertion (bare `assert`,
`self.assert*`, `pytest.raises`), unconditional skip added, workflow edited to ignore its
own failure (`continue-on-error: true`, `|| true`, `--exit-zero`, `if: false`).

Conclusive only in the negative direction — nothing static can show a patch *works*. A
*conditional* skip (`skipif(sys.platform == ...)`) is explicitly **not** cheating: it is
often what the maintainer's own patch does. On the runs actually attempted this decides
**16 of 158 (10.1%)** with no tokens and no variance. (An earlier draft said 70.4%; that
was the unattempted cells being counted as empty patches — see §4.0.)

### 4.2 `oracle_github.py` — the primary oracle
A re-implementation of their loop (§2), so a number produced here means what their
Pass@1 means. Two deliberate departures:

- **Baseline check.** The unpatched failing commit is pushed once per instance and the
  outcome cached. A "pass" is only meaningful against a baseline that failed.
- **`inconclusive` is kept separate** from `failure`. A workflow that was cancelled, or
  whose checks all skipped, validated nothing. `--strict` folds those into failures to
  match their Pass@1; the default drops them. Report both.

### 4.3 `oracle_local.py` — offline fallback
Distils a workflow into shell and runs it under Docker or **Apptainer** (Alliance
clusters forbid Docker). It reports `inconclusive` rather than guessing whenever it
cannot run a workflow faithfully — service containers, unknown marketplace actions.
On our 24 tasks: **17/24 distil to runnable shell**, 7 do not. Use it for cluster batches,
never as the only oracle, and report its instances separately.

### 4.4 `judge.py` — model, demoted and instrumented
Still present, for instances neither of the above can decide, and as a second
measurement. Changes: static pre-screen runs first; the judge is sampled `--samples 3`
times and the **majority wins**, with `agreement` recorded so unstable verdicts are
countable; every verdict carries its provenance.

### 4.5 `agreement.py` — the number that answers the objection
Cohen's κ between the judge and full CI re-execution on runs where both ran, plus
precision/recall and the **per-condition bias**. That last column is the one that
matters: a judge wrong by the same amount in both arms shifts both rates and leaves
their *difference* intact; a judge wrong only under `with_memory` manufactures the
effect.

```
python scripts/score_runs.py --agent claude-code --mode agreement
```

An LLM judge with a published κ against a real oracle is a defensible instrument. One
without is an assertion. Target κ ≥ 0.6 on ≥ 50 runs before quoting any judge-derived
number.

### 4.6 `validate_instances.py` — the check their code skips
Per instance: unpatched must go **red**, gold patch must go **green**. Instances failing
either are excluded from both arms and the exclusion count reported. Two CI runs each,
once, cached. This is their §3.1 criterion, actually executed.

### 4.7 `stats.py` — analysis matched to 5 episodes/task
`paired_rate_analysis` treats **tasks** as the unit of independence. Pooling 5 runs per
task as independent observations inflates the sample size five-fold and narrows every
interval; collapsing to "solved at least once" throws away the difference between 1/5
and 5/5. Each task contributes its success *rate*, and the interval is a cluster
bootstrap over tasks.

## 5. How many tasks are needed

Observed per-task SD of the paired difference, over runs actually attempted: **0.340**.
At 80% power that is **363 tasks to detect 5 points**, and **93 instances exist in the
entire dataset**. The full table, and the withdrawn 0.099/31-task version it replaces,
are in `EXPERIMENT_REPORT.md` §7.9.

**This is the finding that should drive the submission decision, and it points the
opposite way from the version it replaces.** An effect the size this pilot appears to
show cannot be established at any scale this dataset supports, and that is knowable now
from the 158 runs already done rather than after the remaining 322.

Two things follow. Adding runs to the *same* tasks does not help much — the variance that
matters is between tasks, and equalising runs per task (five tasks currently hold 63% of
all attempted runs) matters more than adding any. And the honest submission is the
methodological one: the leakage findings, the metric-failure evidence, and the denominator
case study in §3 above are each supported by the data in hand, and none depends on memory
helping.

Cost is not the constraint. `score_runs.py --mode tokens` puts the full 363-task,
5-episode design at roughly $71 in agent tokens; the execution oracle costs GitHub Actions
minutes, which are free on public repositories. The constraint is that the tasks do not
exist.

## 6. Threats this leaves open

- **Workflow standardization is not semantics-preserving in general.** `on: push` and
  deleting sibling workflows can change behaviour. The §4.6 gold check catches the cases
  where it does; report the exclusion count.
- **Non-determinism in CI itself.** Flaky tests, network installs, and a moving PyPI make
  a re-run a noisy measurement. Their standardized workflows pin the index via a
  `pypi-wayback` service for 61 of 567 instances; `oracle_local.LocalConfig.pip_index`
  supports the same. Re-run the baseline for any instance whose result is surprising.
- **Only Python.** Inherited from the benchmark; state it.
- **`with_memory` prompts are 2.25× longer than `no_memory` prompts** (20,616 vs 9,154
  characters, measured over attempted runs). Any effect could be context length rather
  than memory content, and a two-arm design cannot separate the two. Doing so needs a
  third arm carrying the same volume of real CI history from a different project. Not
  implemented; state the confound wherever an effect is reported.
- **Nothing has been decided by execution yet.** §4.2, §4.3 and §4.6 are written and unrun;
  `ci_conclusion` is empty on all 480 verdict rows and no instance has been checked red-
  before / green-after. Every reported number is judge-derived, which by the standard set in
  §4.5 makes it an assertion rather than a measurement. Two judge false positives are
  already visible without any oracle to find them systematically: on `aisuite 372` the judge
  passed an *invented* `poetry.lock` hash at high confidence, and passed **deleting**
  `poetry.lock` — one in each arm. All 119 stored verdicts are `judge_version: 2`; none was
  produced by the v3 judge described in §4.4.
- **Runs are unbalanced across tasks, and it changes the sign of the result.** Five of 24
  tasks hold 63% of attempted runs. Split by how much evidence each task rests on, tasks
  with fewer than 3 runs per arm give +11.1 pp and tasks with 3 or more give −4.0 pp. Any
  pooled per-task mean is currently a statement about scheduling order. `--mode report`
  prints the split; equalise before quoting the pooled number.
- **Two instances are unsolvable as constructed.** `aisuite 372` and `aisuite 373` need a
  `poetry.lock` content hash regenerated offline. §4.6 validation should exclude them, and
  the exclusion should be reported rather than performed silently.
