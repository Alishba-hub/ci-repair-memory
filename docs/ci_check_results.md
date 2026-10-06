# CI Check: Study Tasks

Date: 2026-10-05 (all 30 tasks checked)

## Short answer

**All 30 study tasks checked: 18 good, 12 bad.**

- **Good (18):** CI fails without the fix and passes with the dataset's fix.
- **Bad (12):** CI still fails with the fix. In most of them the fix *does* repair its own bug, but CI stays
  red because of **other problems**: tools, libraries and websites changed since the dataset was made, and the
  projects do not lock their versions. We call this **environment drift**. Two are dataset mistakes.
- 3 of the 30 are **new**: they fill the 3 spots of camel, whose patches do not match the code. The new tasks are
  conan_549, axolotl_490 and conan_540. Three crewai tasks were tried first and failed (see below).

Per-test details: [ci_test_failures.md](ci_test_failures.md). Categories: [task_categories.md](task_categories.md).

## What we checked

Each task runs on GitHub CI two times, on a fresh machine, with no AI involved:

1. **Without the fix.** CI should **fail**. This shows the bug is real.
2. **With the official fix from the dataset** (`gold_patch.diff`, written by the project's own developers).
   CI should **pass**. This shows the fix works.

Fail first, then pass = **good task**. Anything else = **bad task**.

## Results

| S.No. | Task | Without fix | With fix | Did the fix repair its own bug? | Verdict |
| --- | --- | --- | --- | --- | --- |
| 1 | crb_agno_129 | fail | fail | Can't tell (same errors before and after) | bad |
| 2 | crb_agno_153 | fail | fail | **Yes** (firecrawl import error gone) | bad |
| 3 | crb_agno_164 | fail | fail | Can't tell (same errors before and after) | bad |
| 4 | crb_aider_69 | fail | fail | **Yes** (233 path errors gone) | bad |
| 5 | crb_aider_92 | fail | fail | **Yes** (openrouter test now passes) | bad |
| 6 | crb_aider_94 | fail | fail | **Yes** (committer-name test now passes) | bad |
| 7 | crb_axolotl_459 | fail | **pass** | **Yes** | **good** |
| 8 | crb_axolotl_473 | fail | **pass** | **Yes** | **good** |
| 9 | crb_axolotl_475 | fail | **pass** | **Yes** | **good** |
| 10 | crb_axolotl_490 (new) | fail | **pass** | **Yes** | **good** |
| 11 | crb_browser-use_501 | fail | fail | **Yes** (install works now) | bad |
| 12 | crb_browser-use_504 | fail | **fix could not be applied** | No (dataset mistake) | bad |
| 13 | crb_browser-use_512 | fail | **pass** | **Yes** | **good** |
| 14 | crb_conan_527 | fail | **pass** | **Yes** | **good** |
| 15 | crb_conan_528 | fail | **pass** | **Yes** | **good** |
| 16 | crb_conan_540 (new) | fail | **pass** | **Yes** | **good** |
| 17 | crb_conan_543 | fail | **pass** | **Yes** | **good** |
| 18 | crb_conan_549 (new) | fail | **pass** | **Yes** | **good** |
| 19 | crb_crewai_563 | fail | **pass** | **Yes** | **good** |
| 20 | crb_crewai_564 | fail | **pass** | **Yes** | **good** |
| 21 | crb_crewai_567 | fail | **pass** | **Yes** | **good** |
| 22 | crb_docsgpt_422 | fail | **pass** | **Yes** | **good** |
| 23 | crb_docsgpt_426 | fail | fail | No (tests never run, dataset mistake) | bad |
| 24 | crb_docsgpt_430 | fail | **pass** | **Yes** | **good** |
| 25 | crb_litellm_381 | fail | fail | Can't tell (same error before and after) | bad |
| 26 | crb_litellm_384 | fail | **pass** | **Yes** | **good** |
| 27 | crb_litellm_406 | fail | fail | **Yes** (vertex test now passes) | bad |
| 28 | crb_taipy_437 | fail | **pass** (after re-run) | **Yes** | **good** |
| 29 | crb_taipy_439 | fail | **pass** (after re-run) | **Yes** | **good** |
| 30 | crb_taipy_441 | fail | fail | **Yes** (6 GUI tests fixed) | bad |

**Tried as camel replacements and dropped** (all fail with the fix because of drift):

| S.No. | Task | Without fix | With fix | Why dropped |
| --- | --- | --- | --- | --- |
| 1 | crb_crewai_560 | fail | fail | Fix works, but 2 tests in `task_test.py` fail: `LLM Provider NOT provided` |
| 2 | crb_crewai_573 | fail | fail | Same 2 tests fail: `LLM Provider NOT provided` |
| 3 | crb_crewai_565 | fail | fail | Fix works, but `test_streaming_fallback_to_non_streaming` fails (pydantic validation) |
| 4 | crb_axolotl_495 | - | - | Not run: its patch does not match the code, so it could not be imported |

## Why it failed, per repository

### agno (129, 153, 164): all bad

Two problems break **every** agno run, with or without the fix:

1. **Style check (Ruff).** CI installs the newest Ruff (0.16.x), because no version is locked. New Ruff has
   new rules and finds 7,000 to 10,000 style errors in the whole project.
2. **12 Confluence tests.** CI installs a newer Confluence library whose functions changed, so the tests' fake
   object no longer has them (`Mock has no attribute 'get_all_spaces'`, ...).

- **agno_129:** same failures with and without fix, so the real bug is hidden.
- **agno_153:** without fix, tests crash: `cannot import name 'ScrapeOptions' from 'firecrawl'`. **The fix
  repairs it.** Still red only because of the 2 problems above.
- **agno_164:** same failures with and without fix, so the real bug is hidden.

### aider (69, 92, 94): all bad

Five tests fail in **every** aider run (live website example.com changed its text; newer libraries changed
model data, code parsing and token output). See [ci_test_failures.md](ci_test_failures.md).

- **aider_69:** without fix, 229 tests crash: `aider/io.py` makes a folder for a history file that is `None`.
  **The fix repairs it.**
- **aider_92:** without fix, a test expects an old price (0.0001 vs 100.0). **The fix updates the test.**
- **aider_94:** without fix, commit author name is missing " (aider)". **The fix repairs it.**

### axolotl (459, 473, 475, 490): all good

- **459, 473, 490:** without fix, installing packages fails (`ResolutionImpossible`): `setup.py` picks package
  versions (e.g. `xformers`) made for an older PyTorch, so they clash with PyTorch 2.8 / 2.9. The fix
  changes `setup.py`. With fix: **everything passes.**
- **475:** without fix, `test_lora_packing` fails: setting `dataset_num_proc` is missing. The fix adds it.

### browser-use (501, 504, 512)

- **501 (bad):** without fix, install fails (no `torch` / `sentence-transformers` versions work for all
  Python versions up to 3.14). The fix limits Python to `<3.13`, and install works. Still red: the type-checker
  cannot find the Pillow image library (`Import "PIL" could not be resolved`), which is no longer installed (drift).
- **504 (bad):** **the fix cannot be applied.** It creates `examples/features/multiple_agents_same_browser.py`,
  but at the failing commit that path already exists as a shortcut (symlink). **Dataset mistake.**
- **512 (good):** without fix, style check fails and the type-checker finds 13 errors. With fix: all pass.

### conan (527, 528, 540, 543, 549): all good

- **527, 528, 540:** without fix, functional tests fail: the CI machine now has GCC 13 (tests expect GCC 9)
  and lacks the expected cmake versions. The fix updates the test setup (`test/conftest.py`, meson tests).
- **543:** without fix, unit tests crash: `cannot import name '_relativize_path'`. The fix adds it.
- **549:** without fix, 28 integration tests fail (`list` and `remove` commands): a bug in the server search
  code (`conans/server/rest/controller/v2/search.py`). The fix repairs it and also updates the test setup.

### crewai (563, 564, 567): all good

- **563:** without fix, the type-checker (mypy) finds 5 errors in `src/crewai/llm.py`. The fix repairs them.
- **564:** without fix, the type-checker finds 2 errors in `event_listener.py`. The fix repairs them.
- **567:** without fix, `test_trace_listener_setup_correctly_for_flow` fails (`assert 0 >= 1`). The fix repairs it.

### docsgpt (422, 426, 430)

- **422 (good):** without fix, 4 tests fail: `complete_stream() got an unexpected keyword argument 'retriever'`.
- **426 (bad):** **tests never run**: `No module named pytest`. The CI file installs test tools from
  `tests/requirements.txt`, which does not exist at this commit. The fix only changes `todo_list.py`.
  **Dataset / setup mistake.**
- **430 (good):** without fix, 5 todo-tool tests fail: `string indices must be integers, not 'str'`.

### litellm (381, 384, 406)

- **381 (bad):** the same test fails with and without fix: `LlmProviders has no attribute 'DIGITALOCEAN'`.
  Likely drift: litellm downloads its model list from the internet, and today's list has a provider this old
  code does not know.
- **384 (good):** without fix, 2 `test_url_with_format_param` tests fail (`Expected 'mock' to have been called`).
- **406 (bad):** without fix, a vertex test fails; **the fix repairs it**. With fix, a different test fails:
  `test_get_cost_for_anthropic_web_search` (`assert 0.0 > 0.0`). Likely drift in litellm's online price list.

### taipy (437, 439, 441)

- **437 (good):** without fix, tests crash: `No module named 'pkg_resources'` (newer `setuptools` dropped it),
  and a patch test fails. The fix updates the Pipfile and code. With fix, 1 flaky job
  (`intermittent-tests`, `test_blocked_submittable`) failed once, then **passed on re-run**.
- **439 (good):** without fix, tests crash: `No module named 'pkg_resources'`. The fix removes that need
  (`taipy/rest/commons/apispec.py`). With fix, 1 job hung and was cancelled by its time limit, then
  **passed on re-run**.
- **441 (bad):** without fix, 6 GUI tests fail; **the fix repairs them**. But with fix, all tests crash:
  `No module named 'pkg_resources'`. Its fix changes the Pipfile, which pulls in today's `setuptools`.

## What this means

- The dataset's fixes are **mostly fine**. Of the 30 tasks: 18 pass fully; in 7 more the fix repairs its own
  bug but drift keeps CI red; in 3 we cannot see the bug at all (agno_129, agno_164, litellm_381); 2 are
  dataset mistakes (browser-use_504, docsgpt_426).
- **Main problem: environment drift.** Unlocked tool versions, newer libraries, a changed website.
  It hits all of agno and aider, and parts of browser-use, litellm and taipy.
- If we use "CI is green" as the success rule as-is, **no agent can ever pass the 12 bad tasks**.
- taipy_437 and taipy_439 needed one re-run because of flaky / hanging tests. Agent runs on taipy may need
  the same.

## What we do next

| S.No. | Option | What it means | Good / bad |
| --- | --- | --- | --- |
| 1 | Use only the 18 good tasks | Run the experiment on tasks that are known to be fair | Clean; smaller study, categories uneven |
| 2 | Ignore known-broken tests | Skip the tests that fail with the fix too, judge only the rest | Keeps more tasks; must be reported in the paper |
| 3 | Lock versions | Pin Ruff and libraries to versions from the fix date, then run again | Most faithful; work per repo |
| 4 | Replace the bad tasks | Swap in other dataset tasks and check them on CI | Keeps 30; but agno and aider share the problem |

## Links

| S.No. | Task | Run without fix | Run with fix |
| --- | --- | --- | --- |
| 1 | crb_agno_129 | [run](https://github.com/Alishba-hub/agno/actions/runs/37314901693) | [run](https://github.com/Alishba-hub/agno/actions/runs/35023799755) |
| 2 | crb_agno_153 | [run](https://github.com/Alishba-hub/agno/actions/runs/37314880184) | [run](https://github.com/Alishba-hub/agno/actions/runs/37315126416) |
| 3 | crb_agno_164 | [run](https://github.com/Alishba-hub/agno/actions/runs/37314893827) | [run](https://github.com/Alishba-hub/agno/actions/runs/37315268239) |
| 4 | crb_aider_69 | [run](https://github.com/Alishba-hub/aider/actions/runs/37314879679) | [run](https://github.com/Alishba-hub/aider/actions/runs/37315376465) |
| 5 | crb_aider_92 | [run](https://github.com/Alishba-hub/aider/actions/runs/37314868899) | [run](https://github.com/Alishba-hub/aider/actions/runs/37315560236) |
| 6 | crb_aider_94 | [run](https://github.com/Alishba-hub/aider/actions/runs/37317504940) | [run](https://github.com/Alishba-hub/aider/actions/runs/37318225528) |
| 7 | crb_axolotl_459 | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37317526000) | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37318824243) |
| 8 | crb_axolotl_473 | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37317522418) | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37318755506) |
| 9 | crb_axolotl_475 | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37317539494) | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37319564301) |
| 10 | crb_axolotl_490 | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37324523924) | [run](https://github.com/Alishba-hub/axolotl/actions/runs/37325948153) |
| 11 | crb_browser-use_501 | [run](https://github.com/Alishba-hub/browser-use/actions/runs/37317534494) | [run](https://github.com/Alishba-hub/browser-use/actions/runs/37317587345) |
| 12 | crb_browser-use_504 | [run](https://github.com/Alishba-hub/browser-use/actions/runs/37317544212) | no run (fix could not be applied) |
| 13 | crb_browser-use_512 | [run](https://github.com/Alishba-hub/browser-use/actions/runs/37317559180) | [run](https://github.com/Alishba-hub/browser-use/actions/runs/37318023896) |
| 14 | crb_conan_527 | [run](https://github.com/Alishba-hub/conan/actions/runs/37317541015) | [run](https://github.com/Alishba-hub/conan/actions/runs/37318454583) |
| 15 | crb_conan_528 | [run](https://github.com/Alishba-hub/conan/actions/runs/37317525833) | [run](https://github.com/Alishba-hub/conan/actions/runs/37318560200) |
| 16 | crb_conan_540 | [run](https://github.com/Alishba-hub/conan/actions/runs/37330089932) | [run](https://github.com/Alishba-hub/conan/actions/runs/37332757986) |
| 17 | crb_conan_543 | [run](https://github.com/Alishba-hub/conan/actions/runs/37317529117) | [run](https://github.com/Alishba-hub/conan/actions/runs/37318053938) |
| 18 | crb_conan_549 | [run](https://github.com/Alishba-hub/conan/actions/runs/37324526142) | [run](https://github.com/Alishba-hub/conan/actions/runs/37325127238) |
| 19 | crb_crewai_563 | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37320216939) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37322374244) |
| 20 | crb_crewai_564 | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37320205238) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37320323515) |
| 21 | crb_crewai_567 | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37320193196) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37322397885) |
| 22 | crb_docsgpt_422 | [run](https://github.com/Alishba-hub/DocsGPT/actions/runs/37320110861) | [run](https://github.com/Alishba-hub/DocsGPT/actions/runs/37320639617) |
| 23 | crb_docsgpt_426 | [run](https://github.com/Alishba-hub/DocsGPT/actions/runs/37320138765) | [run](https://github.com/Alishba-hub/DocsGPT/actions/runs/37320527080) |
| 24 | crb_docsgpt_430 | [run](https://github.com/Alishba-hub/DocsGPT/actions/runs/37320098433) | [run](https://github.com/Alishba-hub/DocsGPT/actions/runs/37322366197) |
| 25 | crb_litellm_381 | [run](https://github.com/Alishba-hub/litellm/actions/runs/37320210679) | [run](https://github.com/Alishba-hub/litellm/actions/runs/37322411148) |
| 26 | crb_litellm_384 | [run](https://github.com/Alishba-hub/litellm/actions/runs/37320195769) | [run](https://github.com/Alishba-hub/litellm/actions/runs/37322429787) |
| 27 | crb_litellm_406 | [run](https://github.com/Alishba-hub/litellm/actions/runs/37320232407) | [run](https://github.com/Alishba-hub/litellm/actions/runs/37322384521) |
| 28 | crb_taipy_437 | [run](https://github.com/Alishba-hub/taipy/actions/runs/37320074765) | [run](https://github.com/Alishba-hub/taipy/actions/runs/37327316723) |
| 29 | crb_taipy_439 | [run](https://github.com/Alishba-hub/taipy/actions/runs/37320072538) | [run](https://github.com/Alishba-hub/taipy/actions/runs/37326813399) |
| 30 | crb_taipy_441 | [run](https://github.com/Alishba-hub/taipy/actions/runs/37320087023) | [run](https://github.com/Alishba-hub/taipy/actions/runs/37325789118) |
| - | crb_crewai_560 (dropped) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37324530877) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37324796459) |
| - | crb_crewai_573 (dropped) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37325471925) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37325932097) |
| - | crb_crewai_565 (dropped) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37327578981) | [run](https://github.com/Alishba-hub/crewAI/actions/runs/37328543732) |
