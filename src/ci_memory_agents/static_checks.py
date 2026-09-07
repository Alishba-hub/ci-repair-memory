"""Deterministic checks that settle a run without asking a model.

Two motivations. The first is reliability: every verdict an LLM does not produce is a
verdict that cannot drift between re-runs, and a reviewer asking "how much of your
result rests on a language model" should get the smallest honest number. The second is
that these are the failure modes an LLM judge is worst at. Asking a model whether a
patch removed an assertion is a text-comparison task with a right answer; parsing both
files and counting is exact, and costs nothing.

Nothing here can establish that a patch *works* -- only execution does that. What it
can do is establish that a patch cannot work (it does not parse) or must not count
(it deleted the failing test). Those are conclusive; everything else is passed on.

The checks never see the condition, the run index or the agent name, so they cannot
favour either arm of the experiment.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .patchio import changed_files, walk

# Path shapes that make a file part of the test suite rather than the program.
TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]+\.py$|_test\.py$|conftest\.py$")

SKIP_DECORATORS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "unittest.skip",
    "unittest.expectedFailure",
    "skip",
    "xfail",
)

# A CI configuration edit that makes a red job green without changing the program.
WORKFLOW_WEAKENING = (
    re.compile(r"^\+.*continue-on-error:\s*true", re.MULTILINE),
    re.compile(r"^\+.*\|\|\s*true\s*$", re.MULTILINE),
    re.compile(r"^\+.*--exit-zero", re.MULTILINE),
    re.compile(r"^\+.*if:\s*false", re.MULTILINE | re.IGNORECASE),
)


@dataclass
class StaticReport:
    """What the deterministic checks could establish about one candidate patch.

    `verdict` is "fail" when a check is conclusive against the patch, and "open" when
    nothing conclusive was found and the question is left to an executing oracle or,
    failing that, to the judge. There is deliberately no "pass": no static check can
    show that a repair works.
    """

    verdict: str = "open"
    reason: str = ""
    empty: bool = False
    changed_files: list[str] = field(default_factory=list)
    test_files_touched: list[str] = field(default_factory=list)
    syntax_errors: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    assertions_removed: int = 0
    tests_removed: list[str] = field(default_factory=list)
    skips_added: list[str] = field(default_factory=list)
    unconditional_skips: list[str] = field(default_factory=list)
    workflow_weakened: list[str] = field(default_factory=list)
    suspicions: list[str] = field(default_factory=list)

    @property
    def conclusive(self) -> bool:
        return self.verdict == "fail"

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["conclusive"] = self.conclusive
        return payload


def is_test_file(name: str) -> bool:
    return bool(TEST_PATH.search(name))


def _read(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    parts: list[str] = []
    current: ast.expr | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _is_unconditional_skip(node: ast.expr) -> bool:
    """`@pytest.mark.skip` with no condition, as against `skipif(sys.platform == ...)`.

    A conditional skip narrowly scoped to an unsupported platform is often precisely
    what the maintainer's own patch does, so it is not evidence of anything. An
    unconditional one removes the test from every environment.
    """
    name = _decorator_name(node)
    if name.endswith("skipif") or name.endswith("skipUnless") or name.endswith("skipIf"):
        return False
    return any(name == candidate or name.endswith("." + candidate) for candidate in SKIP_DECORATORS)


def _test_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def _function(self, node) -> None:
            if node.name.startswith("test"):
                found["::".join([*self.scope, node.name])] = node
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

    Visitor().visit(tree)
    return found


def _assertion_count(tree: ast.Module) -> int:
    """Assertions of any flavour: bare `assert`, `self.assert*`, and `pytest.raises`."""
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            total += 1
        elif isinstance(node, ast.Call):
            name = _decorator_name(node.func)
            tail = name.rsplit(".", 1)[-1]
            if tail.startswith("assert") or tail in ("raises", "warns", "fail"):
                total += 1
    return total


def analyse(repo_before: Path, workspace: Path, gold_diff: str = "", diff: str = "") -> StaticReport:
    """Run every deterministic check over one candidate patch.

    `gold_diff` is used only to tell whether the maintainers themselves edited tests. A
    candidate that skips a test is judged differently when the reference patch skips the
    same test, which is a real pattern in this benchmark for platform-specific failures.
    """
    report = StaticReport()
    names = sorted(changed_files(repo_before, workspace))
    report.changed_files = names
    if not names:
        report.empty = True
        report.verdict = "fail"
        report.reason = "the agent changed no files"
        return report

    gold_touches_tests = any(
        is_test_file(line[len("diff --git a/") :].split(" b/")[0])
        for line in (gold_diff or "").splitlines()
        if line.startswith("diff --git a/")
    )
    report.test_files_touched = [name for name in names if is_test_file(name)]

    present = walk(workspace)
    for name in names:
        before_text = _read(repo_before / name)
        after_text = _read(workspace / name)

        if after_text is None and before_text is not None and name not in present:
            report.deleted_files.append(name)
            continue

        if not name.endswith(".py") or after_text is None:
            continue

        after_tree = _parse(after_text)
        if after_tree is None:
            report.syntax_errors.append(name)
            continue

        if before_text is None:
            continue
        before_tree = _parse(before_text)
        if before_tree is None:
            continue  # it did not parse before the agent touched it; not the agent's doing

        if is_test_file(name):
            before_tests = _test_functions(before_tree)
            after_tests = _test_functions(after_tree)
            report.tests_removed += [
                f"{name}::{key}" for key in before_tests if key not in after_tests
            ]
            removed = _assertion_count(before_tree) - _assertion_count(after_tree)
            if removed > 0:
                report.assertions_removed += removed

            for key, node in after_tests.items():
                before_node = before_tests.get(key)
                before_decorators = (
                    {_decorator_name(d) for d in before_node.decorator_list} if before_node else set()
                )
                for decorator in node.decorator_list:
                    name_of = _decorator_name(decorator)
                    if name_of in before_decorators:
                        continue
                    if _is_unconditional_skip(decorator):
                        report.unconditional_skips.append(f"{name}::{key} @{name_of}")
                    elif "skip" in name_of.lower() or "xfail" in name_of.lower():
                        report.skips_added.append(f"{name}::{key} @{name_of}")

    for name in names:
        if name.startswith(".github/workflows/") or name.endswith((".yml", ".yaml")):
            after_text = _read(workspace / name) or ""
            before_text = _read(repo_before / name) or ""
            added = "\n".join(
                "+" + line
                for line in after_text.splitlines()
                if line not in before_text.splitlines()
            )
            for pattern in WORKFLOW_WEAKENING:
                if pattern.search(added):
                    report.workflow_weakened.append(f"{name}: {pattern.pattern}")

    _decide(report, gold_touches_tests)
    return report


def _decide(report: StaticReport, gold_touches_tests: bool) -> None:
    """Turn the observations into a verdict, conservatively.

    Only three things are treated as conclusive. A patch that does not parse cannot
    run. A patch that deletes the test or strips its assertions has hidden the failure
    rather than repaired it, which the benchmark's own framing rules out. A patch that
    edits the workflow to ignore its own failure has done the same at the CI level.

    Adding a skip is not on that list unless it is unconditional and the reference patch
    left the tests alone: a conditional skip for an unsupported platform is a repair,
    and treating it as cheating would mark correct runs wrong.
    """
    if report.syntax_errors:
        report.verdict = "fail"
        report.reason = f"changed file does not parse: {', '.join(report.syntax_errors[:3])}"
        return

    deleted_tests = [name for name in report.deleted_files if is_test_file(name)]
    if deleted_tests:
        report.verdict = "fail"
        report.reason = f"deleted the test file {deleted_tests[0]}"
        return

    if report.tests_removed and not gold_touches_tests:
        report.verdict = "fail"
        report.reason = f"removed {len(report.tests_removed)} test function(s), e.g. {report.tests_removed[0]}"
        return

    if report.assertions_removed > 0 and not gold_touches_tests:
        report.verdict = "fail"
        report.reason = f"removed {report.assertions_removed} assertion(s) from the test suite"
        return

    if report.workflow_weakened:
        report.verdict = "fail"
        report.reason = f"weakened the CI workflow: {report.workflow_weakened[0]}"
        return

    if report.unconditional_skips and not gold_touches_tests:
        report.verdict = "fail"
        report.reason = f"added an unconditional skip: {report.unconditional_skips[0]}"
        return

    if report.unconditional_skips:
        report.suspicions.append(
            "added an unconditional skip, but the reference patch also edits tests"
        )
    if report.skips_added:
        report.suspicions.append(f"added a conditional skip: {report.skips_added[0]}")
    if report.deleted_files:
        report.suspicions.append(f"deleted {len(report.deleted_files)} file(s)")
    report.verdict = "open"
