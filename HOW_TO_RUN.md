# How to run everything

```powershell
cd D:\research\ci-memory-agents
python run.py
```

That is the whole thing. It asks what to run, then runs it:

```
  Which agent?
   > 1. Claude Code                       runs automatically
     2. Copilot                           you drive it in VS Code
  Choose [1]:

  How much do you want to run?
   > 1. Everything                        all 24 tasks
     2. Pick projects                     choose from a list
     3. A number of tasks                 spread across projects
     4. Just one task                     smallest possible check
  Choose [1]: 2

   #   project                  tasks   runs done
    1. agentscope                   1          20
    2. agno                         1          20
   ...
   Enter numbers (3), lists (3,7,9), ranges (3-6), a name, or all
  Projects [all]: agno, httpx

  Runs per condition (1-20) [5]: 5
  How many runs at once (1-12) [6]: 4
```

Then it shows exactly what will happen before touching anything:

```
Here is what will happen
  agent                  claude-code
  tasks                  2 of 24
                           crb_agno_180
                           crb_httpx_41
  conditions             no_memory, memory_k1, memory_k3, memory_k5
  runs per condition     5
  cells in total         40
  already run            12
  the agent will run     28
  roughly 25 minutes at 4 at a time

  Same thing without the questions next time:
  python run.py --project agno --project httpx --runs 5 --parallel 4

  Start (y/n) [y]:
```

**It prints the flags it chose**, so the first run teaches you the command for the next
one. Press Enter through everything to run all 24 tasks with the standard settings.

`run.py` does the five steps in the right order, finds the agent binary itself, and picks
the defaults that used to be easy to get silently wrong. Check the machine first with
`python run.py doctor`.

Everything is resumable and nothing is destructive. Each step skips work that is already
done, and no step will overwrite a run an agent has already completed. If a batch is
interrupted, run the same command again.

---

## Choosing what to run, without the questions

The menu is only a front end for flags. Pass any of them and it skips straight to running
— which is also what happens in a script or over SSH, where blocking on input would hang.

`python run.py tasks` lists the projects and how much of each is done:

```
  project                  tasks   run  judged   task ids
  agentscope                   1    20      20   342
  agno                         1    20      20   180
  aws-cli                      2     8       8   144, 146
  ...
```

Then narrow it however you like. The same filter applies to every step, so laying out,
running and scoring always act on the same set.

| What you want | Command |
|---|---|
| One project | `python run.py --project agno` |
| Several projects | `python run.py --project agno --project httpx` |
| Six tasks, spread across projects | `python run.py --tasks 6` |
| One task from one project | `python run.py --project conan --tasks 1` |
| One exact task | `python run.py --task-id crb_aider_97` |
| Fewer repeats per condition | `python run.py --tasks 6 --runs 3` |
| See what would happen first | `python run.py --tasks 6 --dry-run` |

`--tasks N` spreads across projects rather than taking the first N in order. Taking them
in order would spend a small budget entirely on whichever project sorts first, and a
study of one project is not the study.

**Use `--dry-run` when unsure.** It prints the tasks, the cell count, how many are already
done and roughly how long the rest will take, then stops without changing anything.

```
Running 2 of 24 tasks
---------------------
  crb_httpx_41
  crb_kitty_313                   2 runs already done

  2 tasks x 4 conditions x 5 runs = 40 cells

Dry run -- nothing was changed
------------------------------
  cells planned          40
  already run            4
  the agent would run    36
  roughly 20 minutes at 6 at a time
```

---

## The commands

| Command | What it does |
|---|---|
| `python run.py` | Everything not yet done: check fairness, lay out cells, run the agent, judge, report |
| `python run.py tasks` | List projects and pick what to run |
| `python run.py status` | Progress, and whether the result is defensible yet |
| `python run.py doctor` | Check this machine, change nothing |
| `python run.py results` | Just print the results table |
| `python run.py judge` | Judge finished runs, then report |
| `python run.py verify` | Decide success by really running CI — see below |
| `python run.py dashboard` | Browser view at localhost:8004 |
| `python run.py tokens` | What it has cost, what the reductions saved, what the full study would cost |
| `python run.py clean` | Delete cells no agent has run; runs with results are never touched |

Useful options: `--runs N` (repeats per condition, default 5), `--parallel N` (default 6),
`--timeout N` (seconds per run, default 900), `--agent copilot`, `--exe <path>` if the
agent binary is not found automatically, `--quick` for a two-run smoke test on one task.

---

## Reading the result

`python run.py status` answers the only question that matters — is this defensible yet?

```
Is the result defensible yet?
  yes  runs exist
   no  everything judged                             python run.py judge
   no  instances proven red-before / green-after     python run.py verify
   no  success decided by executing CI, not inferred python run.py verify
```

The results table itself prints, in order: a coverage ladder (planned → attempted → timed
out → decided), which oracle decided each run, the rate per condition, a split by
repository scope, the paired analysis over tasks, the power calculation, and a run-balance
split.

Three things to actually read:

1. **The coverage ladder.** Rates are over *decided* runs. Cells nobody has run are
   excluded — they are missing data, not failed repairs. Confusing the two is what once
   made the same 158 runs support both a 71% repair rate and a 17% one.
2. **Which oracle decided what.** A rate whose provenance is mostly `judge` is a weaker
   claim than one decided by execution, and the table says which.
3. **The run-balance split.** If the `<3 runs/arm` and `3+ runs/arm` lines disagree in
   sign, the pooled number is describing scheduling order, not memory. Fix the balance
   before quoting it.

---

## Saving the results as CSV

Every number the three research questions need, written to files, so a later analysis
never has to re-run an agent:

```
python scripts/export_results.py
```

It reads `runs/verdicts_<agent>.jsonl` for every agent with runs on disk and writes
seven files into `results/`:

| File | What it holds |
|---|---|
| `runs.csv` | one row per run — the long-format table everything else is derived from |
| `rq1_memory_effect.csv` | RQ1: memory vs no memory, paired per task, per agent, with McNemar |
| `rq2_consistency.csv` | RQ2: solved-runs out of runs for every cell, plus its spread |
| `rq3_memory_size.csv` | RQ3: success rate and Pass@1 at each K, and the delta against K=0 |
| `by_error_group.csv` | RQ1 split across the three problem groups |
| `by_repo.csv` | RQ1 split across the ten repositories |
| `population.csv` | which tasks were studied, and what each one is |

Run it after `score_runs.py --mode report`, which is what produces the verdicts. If an
agent has no verdicts file the script names it and carries on with the rest rather than
failing.

Two columns in `runs.csv` deserve attention. `oracle` says how each run was decided —
`execution` (the workflow was re-run), `static` (a deterministic check), or `judge` (a
model's opinion). A rate whose provenance is mostly `judge` is a weaker claim than the
same rate from execution, and pooling them without saying so is how the two get quoted
as though they were one number. `analysis_condition` is the arm the row is *analysed*
as, next to the raw on-disk `condition`: runs collected under the old `with_memory` name
are folded onto `memory_k3`, the same treatment. Pass `--strict-arms` to drop them
instead, at the cost of a much smaller sample.

Rates in every file except `runs.csv` are over runs an oracle actually decided.
Unattempted cells are excluded rather than scored as failures — the harness materialises
a prompt and a pristine workspace for every *planned* run, and counting those is the
defect that once moved this project's reported rate from 57% to 17%.

## What it costs

```powershell
python run.py tokens
```

```
  condition     what       calls     input   output      cost  source
  no_memory     agent         79    195.4k    40.7k     $1.20  estimated
  memory_k1     agent         79    305.2k    39.4k     $1.51  estimated
  memory_k3     agent         79    440.1k    38.1k     $1.89  estimated
  memory_k5     agent         79    588.7k    38.9k     $2.31  estimated
  TOTAL                      158    635.5k    78.7k     $3.09  estimated

  design                                    runs   projected
  the current plan, finished                 480       $9.38
  5 pp at 80% power, 5 episodes             3630      $70.93
```

Reported per condition because memory is not free: the `memory_k3` prompt is 2.25x the
`no_memory` one and `memory_k5` is roughly 3x, and each costs proportionally more per
run. The cost is the reason RQ3 matters: if K=1 buys most of the benefit of K=5, the
study's recommendation is K=1. A benefit claimed without its cost
beside it invites the reader to assume there was none.

Figures marked `estimated` come from character counts, not from the CLI. They are
planning numbers and are labelled as such wherever they appear; measured and estimated
figures are never summed into one unlabelled total.

Projections are per *run*, not per task. The tasks measured so far are unevenly filled,
so a per-task average over them would describe the current lopsided sample rather than a
complete one.

### Three ways the harness spends fewer tokens

None of them changes a verdict. Each skips a question whose answer is already known.

| Technique | What it skips | Saving here |
|---|---|---|
| Deterministic pre-screen | Empty patches, unparseable files, deleted tests, weakened workflows never reach a model | 16 of 158 runs |
| Identical-patch reuse | Agents converge; 26 of 144 candidate patches are byte-identical to another run's, one repeated 16 times. The judging prompt is a pure function of its inputs, so an identical prompt has an answer already paid for | 18% of judging calls |
| Early-stop sampling | Self-consistency needs a majority, not a fixed count. Two agreeing samples out of three already settle it, so the third is drawn only on disagreement | a further ~28% of calls |

Together: a full re-judge drops from **432 calls and ~1.17M input tokens to ~253 calls
and ~0.70M**, with identical results.

Reuse is on by default. Turn it off with `judge_runs.py --no-reuse` when you want to
measure the judge's own variance across repeats rather than save money — that is the one
case where asking the same question twice is the point.

---

## Copilot

Copilot has no CLI. `python run.py --agent copilot` lays out the cells and then tells you:

1. `Ctrl+Shift+P` → **Developer: Reload Window**
2. `Ctrl+Shift+P` → **CI Memory: Run All Experiments**
3. `python run.py judge --agent copilot`

Settings live under `Ctrl+,` → search `ciMemory`.

---

## The two steps that make the result real

Everything above rests on a model's judgement. Two things are needed before any number
here counts as measured rather than inferred, and `status` tracks both.

### `python run.py verify` — the execution oracle

The benchmark's own oracle is to re-run the failing workflow and require every check to
pass. **No run in this repository has been decided that way yet**, which is why the
results table prints a warning. It costs GitHub Actions minutes — free on public
repositories — and no model tokens.

```powershell
$env:GITHUB_TOKEN="..."       # needs repo + workflow scope
$env:GITHUB_USERNAME="..."
$env:BENCHMARK_OWNER="..."
python run.py verify
```

That validates each instance is red unpatched and green with the gold patch, re-executes
CI with each candidate patch, and reports Cohen's κ between the judge and real CI. Read
the **per-condition** column: a judge wrong by the same amount in both arms shifts both
rates and leaves their difference intact; one wrong only under `memory_k3` manufactures
the effect. Target **κ ≥ 0.6 on ≥ 50 runs** before quoting any judge-derived number.

Offline, for clusters that forbid Docker: `python run.py verify --backend local`. It
distils the workflow into shell and reports `inconclusive` rather than guessing when it
cannot run one faithfully. 17 of 24 tasks distil. Report those instances separately.


---

## Starting over

```powershell
python scripts\import_ci_repair_bench.py --limit 24 --max-per-project 2
python run.py doctor
```

To give the agent real repositories instead of only the files the fix touches — which is
what makes fault localization part of the task rather than a giveaway:

```powershell
python scripts\materialize_repos.py --plan            # disk cost first
python scripts\materialize_repos.py --max-checkout-mb 500
python run.py                                          # workspaces refresh automatically
```

Runs against the two scopes cannot be pooled; the results table splits them.

---

## Before a long batch

Turn off sleep: Settings → System → Power → put my device to sleep → **Never**. A sleeping
machine kills runs mid-edit; those are recorded as timeouts and excluded from scoring, so
the work is wasted.

---

## Under the hood

`run.py` is a wrapper. The individual scripts still work and take the same `--project`,
`--tasks` and `--task-id` filters:

| Script | What it does |
|---|---|
| `scripts/audit_leakage.py` | Proves no task hands the memory arm its own answer |
| `scripts/run_experiment.py` | Lays out prompts and workspaces; also the textual diagnostics |
| `scripts/auto_run.py` | Runs the agent over pending cells, in parallel, resumable |
| `scripts/judge_runs.py` | The model judge |
| `scripts/score_runs.py` | **The scorer.** Oracle precedence, coverage, rates, power |
| `scripts/validate_instances.py` | Proves each instance is red before repair |
| `scripts/materialize_repos.py` | Replaces gold-file trees with real checkouts |
| `scripts/dashboard.py` | The browser view |

One rule if you call them directly: **use `score_runs.py --mode report`, not
`judge_runs.py --mode report`.** Only the first knows the difference between a run that
happened and a cell that was never run.
