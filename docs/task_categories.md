# Task Categories

Date: 2026-10-05

The 30 study tasks are split into 3 problem categories, based on the kind of error that broke CI.

Each task was run on CI twice: **without the fix** and **with the dataset's fix**.
All 30 tasks **fail** without the fix (the bug is real). The columns below show the CI result **with the fix**:

- **Pass** = CI passes with the fix. The task is usable.
- **Fail** = CI still fails with the fix. The task is not usable as it is.

## Summary

| S.No. | Category | What it means | Tasks | With fix: Pass | With fix: Fail |
| --- | --- | --- | --- | --- | --- |
| 1 | Code / runtime errors | The code itself is broken: syntax errors, type errors, crashes while running | 8 | 4 | 4 |
| 2 | Test / assertion failures | A test runs but gives the wrong result | 12 | 7 | 5 |
| 3 | Dependency / environment errors | Install or setup breaks: packages don't install, wrong versions, config problems | 10 | 7 | 3 |
| | **Total** | | **30** | **18** | **12** |

## All tasks

| S.No. | Task | Category | Error type (from dataset) | Without fix | With fix |
| --- | --- | --- | --- | --- | --- |
| 1 | crb_crewai_563 | Code / runtime | Type Checking Error | Fail | Pass |
| 2 | crb_crewai_564 | Code / runtime | Type Checking Error | Fail | Pass |
| 3 | crb_crewai_567 | Code / runtime | Runtime Error | Fail | Pass |
| 4 | crb_docsgpt_422 | Code / runtime | Runtime Error | Fail | Pass |
| 5 | crb_agno_129 | Code / runtime | Dependency Issues, Syntax Error | Fail | Fail |
| 6 | crb_agno_153 | Code / runtime | Dependency Issues, Syntax Error | Fail | Fail |
| 7 | crb_agno_164 | Code / runtime | Syntax Error, Dependency Issues | Fail | Fail |
| 8 | crb_aider_69 | Code / runtime | Runtime Error, Syntax Error | Fail | Fail |
| 9 | crb_conan_527 | Test / assertion | Environment Error, Test Failure | Fail | Pass |
| 10 | crb_conan_528 | Test / assertion | Environment Error, Test Failure, Syntax Error | Fail | Pass |
| 11 | crb_conan_540 (new) | Test / assertion | Environment Error, Test Failure | Fail | Pass |
| 12 | crb_conan_549 (new) | Test / assertion | Test Failure, Runtime Error | Fail | Pass |
| 13 | crb_docsgpt_430 | Test / assertion | Runtime Error, Test Failure, Configuration Error | Fail | Pass |
| 14 | crb_litellm_384 | Test / assertion | Test Failure, Runtime Error, Configuration Error | Fail | Pass |
| 15 | crb_taipy_437 | Test / assertion | Test Failure, Dependency Issues | Fail | Pass (flaky job passed on re-run) |
| 16 | crb_aider_92 | Test / assertion | Assertion Error | Fail | Fail |
| 17 | crb_aider_94 | Test / assertion | Assertion Error | Fail | Fail |
| 18 | crb_litellm_381 | Test / assertion | Runtime Error, Test Failure | Fail | Fail |
| 19 | crb_litellm_406 | Test / assertion | Runtime Error, Test Failure | Fail | Fail |
| 20 | crb_taipy_441 | Test / assertion | Test Failure, Dependency Issues, Runtime Error | Fail | Fail |
| 21 | crb_axolotl_459 | Dependency / environment | Package Installation Error | Fail | Pass |
| 22 | crb_axolotl_473 | Dependency / environment | Package Installation Error | Fail | Pass |
| 23 | crb_axolotl_475 | Dependency / environment | Dependency Issues | Fail | Pass |
| 24 | crb_axolotl_490 (new) | Dependency / environment | Package Installation Error | Fail | Pass |
| 25 | crb_browser-use_512 | Dependency / environment | Code Formatting, Dependency Issues, Code Linting | Fail | Pass |
| 26 | crb_conan_543 | Dependency / environment | Dependency Issues | Fail | Pass |
| 27 | crb_taipy_439 | Dependency / environment | Dependency Issues | Fail | Pass (stuck job passed on re-run) |
| 28 | crb_browser-use_501 | Dependency / environment | Package Installation Error, Configuration Error | Fail | Fail |
| 29 | crb_browser-use_504 | Dependency / environment | Configuration Error, Code Linting | Fail | Fail |
| 30 | crb_docsgpt_426 | Dependency / environment | Package Installation Error, Dependency Issues | Fail | Fail |

"New" = one of the 3 tasks added to fill the camel spots.

## Note on balance

The plan was 10 tasks per category. It is now 8 / 12 / 10. The 3 new tasks had to come from conan and axolotl,
which have no code/runtime candidates. The crewai candidates (560, 573, 565) all failed CI with the fix, and
the only other code/runtime candidates are in agno, which fails because of the newer style checker.

Why each task failed: [ci_test_failures.md](ci_test_failures.md). Overview: [ci_check_results.md](ci_check_results.md).
