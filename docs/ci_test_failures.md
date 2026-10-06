# Why Each Test Failed: Study Tasks

Date: 2026-10-05 (all 30 tasks checked)

Each task ran on CI twice: **without the fix** and **with the dataset's fix**.
"Real bug" = the failure the fix is meant to repair. "Drift" = breaks because tools, libraries or websites
are newer today. The fix cannot repair drift.

Overview and verdicts: [ci_check_results.md](ci_check_results.md).

## Summary

| S.No. | Task | Without fix | With fix | Real bug fixed? |
| --- | --- | --- | --- | --- |
| 1 | crb_agno_129 | 12 tests + style check fail | same 12 tests + style check fail | can't see the bug |
| 2 | crb_agno_153 | test run crashes (1 error) + style check | 12 tests + style check fail | **yes** |
| 3 | crb_agno_164 | 12 tests + style check fail | same 12 tests + style check fail | can't see the bug |
| 4 | crb_aider_69 | 229 tests + 5 errors fail | 5 tests fail | **yes** |
| 5 | crb_aider_92 | 6 tests fail | 5 tests fail | **yes** |
| 6 | crb_aider_94 | 6 tests fail | 5 tests fail | **yes** |
| 7 | crb_axolotl_459 | install fails | **all pass** | **yes** |
| 8 | crb_axolotl_473 | install fails | **all pass** | **yes** |
| 9 | crb_axolotl_475 | 1 test fails (in all 6 jobs) | **all pass** | **yes** |
| 10 | crb_axolotl_490 (new) | install fails | **all pass** | **yes** |
| 11 | crb_browser-use_501 | install fails (3 jobs) | type-checker fails (4 errors) | **yes** |
| 12 | crb_browser-use_504 | style + type-checker fail | fix could not be applied | no CI run |
| 13 | crb_browser-use_512 | style + type-checker fail | **all pass** | **yes** |
| 14 | crb_conan_527 | 7 tests + 238 errors | **all pass** | **yes** |
| 15 | crb_conan_528 | 4 tests + 252 errors | **all pass** | **yes** |
| 16 | crb_conan_540 (new) | 4 tests + 238 errors | **all pass** | **yes** |
| 17 | crb_conan_543 | 1 error (unit tests crash) | **all pass** | **yes** |
| 18 | crb_conan_549 (new) | 28 tests fail | **all pass** | **yes** |
| 19 | crb_crewai_563 | type-checker: 5 errors | **all pass** | **yes** |
| 20 | crb_crewai_564 | type-checker: 2 errors | **all pass** | **yes** |
| 21 | crb_crewai_567 | 1 test fails | **all pass** | **yes** |
| 22 | crb_docsgpt_422 | 4 tests fail | **all pass** | **yes** |
| 23 | crb_docsgpt_426 | tests never run (no pytest) | tests never run (no pytest) | no CI test run |
| 24 | crb_docsgpt_430 | 5 tests fail | **all pass** | **yes** |
| 25 | crb_litellm_381 | 1 test fails | same 1 test fails | can't see the bug |
| 26 | crb_litellm_384 | 2 tests fail | **all pass** | **yes** |
| 27 | crb_litellm_406 | 1 test fails | a different test fails | **yes** |
| 28 | crb_taipy_437 | tests crash + 1 test fails | **all pass** (after re-run of 1 flaky job) | **yes** |
| 29 | crb_taipy_439 | tests crash | **all pass** (after re-run of 1 hung job) | **yes** |
| 30 | crb_taipy_441 | 6 tests fail | all tests crash (no `pkg_resources`) | **yes** |

"(new)" = added to fill the 3 camel spots.

## agno (129, 153, 164)

### Style check (`style-check (3.9)`): fails in all 6 runs (drift)

| Check | Why it failed |
| --- | --- |
| Ruff check | CI installs the newest Ruff (0.16.x). New Ruff has new rules and finds 7,290 to 9,910 style errors in the whole project. The old code was written for an older Ruff. |

### Tests (`tests (3.12)`): the same 12 tests fail in 5 of 6 runs (drift)

All in `tests/unit/tools/test_confluence.py`.

| Test | Error | Why it failed |
| --- | --- | --- |
| test_get_all_space_detail | `Mock has no attribute 'get_all_spaces'` | A newer Confluence library changed its functions. The test's fake object no longer has this one. |
| test_get_space_key_existing | `Mock has no attribute 'get_all_spaces'` | Same reason |
| test_get_space_key_not_found | `Mock has no attribute 'get_all_spaces'` | Same reason |
| test_get_page_content_success | `Mock has no attribute 'get_page_by_title'` | Same reason |
| test_get_page_content_not_found | `Mock has no attribute 'get_page_by_title'` | Same reason |
| test_get_page_content_error | `Mock has no attribute 'get_page_by_title'` | Same reason |
| test_get_all_page_from_space | `Mock has no attribute 'get_all_pages_from_space'` | Same reason |
| test_create_page_success | `Mock has no attribute 'create_page'` | Same reason |
| test_create_page_with_parent | `Mock has no attribute 'create_page'` | Same reason |
| test_create_page_error | `Mock has no attribute 'create_page'` | Same reason |
| test_update_page_success | `Mock has no attribute 'update_page'` | Same reason |
| test_update_page_error | `Mock has no attribute 'update_page'` | Same reason |

### crb_agno_153, without fix only (real bug)

| Test | Error | Why it failed |
| --- | --- | --- |
| test_firecrawl.py (whole file, could not load) | `ImportError: cannot import name 'ScrapeOptions' from 'firecrawl'` | The code uses a name that the firecrawl library no longer has. The test run stops early, so the Confluence tests never even run. **The fix repairs this.** |

### crb_agno_129 and crb_agno_164

Same failures with and without the fix (only the drift above). The real bug cannot be seen.

## aider (69, 92, 94)

### crb_aider_69, without fix only (real bug)

| Tests | Error | Why it failed |
| --- | --- | --- |
| 229 tests + 5 errors in 14 files (test_coder, test_commands, test_io, test_repo, test_repomap, ...) | `TypeError: expected str, bytes or os.PathLike object, not NoneType` | `aider/io.py` always tries to make a folder for the input history file, even when there is no file (`None`). Almost every test creates this object, so they all crash. **The fix adds a "skip if no file" check.** |

### crb_aider_92, without fix only (real bug)

| Test | Error | Why it failed |
| --- | --- | --- |
| test_openrouter_get_model_info_from_cache | `assert 100.0 == 0.0001` | The code was changed to return the price as 100.0, but the test still expects the old value 0.0001. The test is out of date. **The fix updates the test's expected values.** |

### crb_aider_94, without fix only (real bug)

| Test | Error | Why it failed |
| --- | --- | --- |
| test_commit_with_custom_committer_name | `'Test User' != 'Test User (aider)'` | The commit author name is missing the " (aider)" tag the test expects. **The fix repairs this.** |

### All 3 aider tasks, with and without fix (drift)

| Test | Error | Why it failed |
| --- | --- | --- |
| test_scrape_actual_url_with_playwright | `'Example Domain' not found` | The test opens the live website example.com and looks for the words "Example Domain". The website has changed its text. |
| test_max_context_tokens | `KeyError: 'max_input_tokens'` | A newer model-info library changed its data, so this field is missing. |
| test_language_csharp | `1 not greater than 1` | Code-map finds nothing in C# code. Likely a newer code-parser library (tree-sitter). |
| test_cmd_tokens_output | `False is not true` | The token report output changed. Likely a newer library. |
| test_cmd_read_only_with_image_file | `0 != 1` | Expected 1 image file added, got 0. Likely a newer library. |

"Likely" = not yet checked line by line.

## browser-use (501, 504, 512)

### crb_browser-use_501

| When | Job / test | Error | Why it failed |
| --- | --- | --- | --- |
| Without fix | code-style, syntax-errors, type-checker (all 3 jobs) | `No solution found when resolving dependencies` | The install step fails. The project allows Python up to 3.14, and there are no `torch` / `sentence-transformers` versions that work on all of them. **Real bug. The fix limits Python to `<3.13`, and install works.** |
| With fix | type-checker (pyright), 4 errors in `browser_use/agent/gif.py` | `Import "PIL" could not be resolved` | The Pillow image library is not installed. It is not a direct dependency; it probably came in through another library before and no longer does (drift). |

### crb_browser-use_504

| When | Job / test | Error | Why it failed |
| --- | --- | --- | --- |
| Without fix | code-style + type-checker (13 errors) | same as browser-use_512 below | Real bug |
| With fix | none: **CI never ran** | `patch does not apply` | The fix creates a new file `examples/features/multiple_agents_same_browser.py`. At the failing commit, that path already exists as a shortcut (symlink). Git sees a clash and stops. **Mistake in the dataset.** |

### crb_browser-use_512 (good task)

Without fix (real bug): code-style fails, and the type-checker (pyright) finds 13 errors. With fix: **all pass.**

| File | Error |
| --- | --- |
| browser_use/llm/anthropic/chat.py (4 errors) | `No overloads for "create" match the provided arguments`; wrong argument type for the system message |
| browser_use/llm/aws/chat_anthropic.py (5 errors) | `"AsyncAnthropicBedrock" is not exported from module "anthropic"`; same `create` errors |
| browser_use/dom/playground/test_accessibility.py | `Cannot access attribute "accessibility" for class "Page"` |
| browser_use/llm/google/serializer.py | Wrong argument type `Content` |
| eval/judge_system.py (2 errors) | Wrong argument type `ImagingCore` |

## conan (527, 528, 540, 543, 549): all good tasks

### crb_conan_527, crb_conan_528 and crb_conan_540, without fix only (real bug)

The CI machine changed, and the tests were not updated for it yet. The fix updates the test setup
(`test/conftest.py` and the meson test base).

| Tests | Error | Why it failed |
| --- | --- | --- |
| 238 (527, 540) / 252 (528) test errors, e.g. test_install_deploy, test_editable_cmake_linux | `Required 'cmake' tool version '3.19' is not available` | The tests look for cmake versions that the new CI machine does not have. |
| 4 meson tests: test_definition_of_global_options, test_reuse (x2), test_build | `'main __GNUC__9' not found` (output shows `__GNUC__13`) | Tests expect the GCC 9 compiler. The machine now has GCC 13. |
| 3 tests in package_manager_test.py (527 only): test_apt_check, test_apt_install_substitutes, test_build_require | `'dpkg-query: no packages found matching non-existing1' not in output` | The system package tool on the new machine prints a different message. |

With fix: **all pass.**

### crb_conan_543, without fix only (real bug)

| Test | Error | Why it failed |
| --- | --- | --- |
| unit test run (could not load) | `ImportError: cannot import name '_relativize_path' from 'conan.tools.google.bazeldeps'` | A test uses a function that does not exist yet in `bazeldeps.py`. The test run stops, and the other jobs are cancelled. **The fix adds the function.** |

With fix: **all pass.**

### crb_conan_549, without fix only (real bug)

| Tests | Error | Why it failed |
| --- | --- | --- |
| 28 integration tests, mostly `list_test.py` (12) and `remove_test.py` (11) | e.g. `assert {'bar/1.1#78b...'} == set()`, `KeyError: 'content'` | Bug in the server search code (`conans/server/rest/controller/v2/search.py`): `conan list` / `conan remove` get wrong results. **The fix repairs it** (and also updates the test setup for the new machine). |

With fix: **all pass.**

## axolotl (459, 473, 475, 490): all good tasks

| Task | Without fix: step / test | Error | Why it failed |
| --- | --- | --- | --- |
| axolotl_459 | Install dependencies (PyTorch 2.8 and 2.9 jobs) | `ResolutionImpossible` (axolotl needs `torch==2.8.0`, other packages clash) | `setup.py` picks an `xformers` version made for an older PyTorch. **The fix picks the right `xformers` for PyTorch 2.8 / 2.9.** |
| axolotl_473 | Install dependencies (PyTorch 2.8 and 2.9 jobs) | `ResolutionImpossible` | Same as 459. **The fix adds `xformers` versions for PyTorch 2.8, 2.9 and 2.10.** |
| axolotl_490 | Install dependencies (PyTorch 2.8 and 2.9 jobs) | `ResolutionImpossible` | Same kind of version clash. **The fix changes `setup.py`.** |
| axolotl_475 | test_lora_packing (all 6 jobs) | `'AxolotlTrainingArguments' object has no attribute 'dataset_num_proc'` | The code reads a setting that does not exist. **The fix adds the `dataset_num_proc` setting.** |

With fix: **all pass** for all 4.

## crewai (563, 564, 567): all good tasks

| Task | Without fix: job / test | Error | Why it failed |
| --- | --- | --- | --- |
| crewai_563 | type-checker (mypy) | `Found 5 errors in 1 file` | Type errors in `src/crewai/llm.py`. **The fix repairs them.** |
| crewai_564 | type-checker (mypy) | `Found 2 errors in 1 file` | Type errors in `utilities/events/event_listener.py`. **The fix repairs them.** |
| crewai_567 | test_trace_listener_setup_correctly_for_flow | `assert 0 >= 1` | The trace listener is not set up for flows. **The fix repairs it.** |

With fix: **all pass.**

### crewai tasks tried as replacements and dropped

| Task | When | Test | Error | Why it failed |
| --- | --- | --- | --- | --- |
| crewai_560 | Without fix | test_embedding_configuration.py (could not load) | `No module named 'crewai.utilities.embedding_configurator'` | Real bug. **The fix repairs it.** |
| crewai_560 | With fix | test_increment_tool_errors, test_output_pydantic_to_another_task | `LLM Provider NOT provided` | Likely drift: newer litellm no longer accepts the model name the tests use. |
| crewai_573 | Both | same 2 tests as above | `LLM Provider NOT provided` | Same drift. (Its own bug, `'str' object has no attribute 'get'`, is fixed.) |
| crewai_565 | Without fix | 2 tests in test_llm.py | `LLM Provider NOT provided` | Real bug. **The fix updates these tests.** |
| crewai_565 | With fix | test_streaming_fallback_to_non_streaming | `ValidationError for LLMStreamChunkEvent` | Likely drift: newer library version. |

## docsgpt (422, 426, 430)

| Task | When | Test | Error | Why it failed |
| --- | --- | --- | --- | --- |
| docsgpt_422 (good) | Without fix | 4 tests in `api/answer/routes/test_base.py` | `complete_stream() got an unexpected keyword argument 'retriever'` | Code and tests disagree on the function's inputs. **The fix repairs it.** With fix: all pass. |
| docsgpt_426 (bad) | Both | none run | `No module named pytest` | The CI file installs test tools from `tests/requirements.txt`, which does not exist at this commit. Tests never run. **Dataset / setup mistake.** |
| docsgpt_430 (good) | Without fix | 5 tests in `test_todo_tool.py` | `string indices must be integers, not 'str'` | The todo tool reads data in the wrong shape. **The fix repairs it.** With fix: all pass. |

## litellm (381, 384, 406)

| Task | When | Test | Error | Why it failed |
| --- | --- | --- | --- | --- |
| litellm_381 (bad) | Both | test_completion_github_copilot_mock_response | `LlmProviders has no attribute 'DIGITALOCEAN'` | Likely drift: litellm downloads its model list online, and today's list has a provider this old code does not know. |
| litellm_384 (good) | Without fix | 2 `test_url_with_format_param` tests | `Expected 'mock' to have been called` | Real bug. **The fix repairs it.** With fix: all pass. |
| litellm_406 (bad) | Without fix | test_completion_pydantic_obj_2 | request body mismatch | Real bug. **The fix repairs it.** |
| litellm_406 (bad) | With fix | test_get_cost_for_anthropic_web_search | `assert 0.0 > 0.0` | Likely drift: cost comes from litellm's online price list, which changed. |

## taipy (437, 439, 441)

| Task | When | Test / job | Error | Why it failed |
| --- | --- | --- | --- | --- |
| taipy_437 (good) | Without fix | many jobs; test_patch_change_list_by_list | `No module named 'pkg_resources'`; `assert [0, 1, 2] == [0, -1, -2, -3]` | Newer `setuptools` dropped `pkg_resources`, and a patch test fails. **The fix updates the Pipfile and code.** |
| taipy_437 (good) | With fix, attempt 1 | intermittent-tests: test_blocked_submittable | `AssertionError` | Flaky timing test (the job is named "intermittent"). **Passed on re-run.** |
| taipy_439 (good) | Without fix | many jobs | `No module named 'pkg_resources'` | Real bug. **The fix removes the need for it** (`taipy/rest/commons/apispec.py`). |
| taipy_439 (good) | With fix, attempt 1 | overall-tests (3.11, max) | job cancelled | Hung at about 47% of the tests and hit its time limit. **Passed on re-run.** |
| taipy_441 (bad) | Without fix | 6 GUI tests (image, indicator, metric, menu) | e.g. `display="{!12.0 not in <Part ...>` | Real bug. **The fix updates these tests.** |
| taipy_441 (bad) | With fix | all test jobs | `No module named 'pkg_resources'` | The fix changes the Pipfile, so CI reinstalls packages and gets today's `setuptools`, which dropped `pkg_resources`. Drift. |

## Note

In aider (and in conan_543), CI cancels the other jobs once one fails. So those failures come from the first
job that failed in each run.
