"""Standardising a benchmark instance's workflow so it can be re-executed.

This reproduces what CI-Repair-Bench's harness does to a workflow before it re-runs
it, taken from the released code rather than from the paper, because the two differ.

`benchmark_functions.copy_and_edit_workflow_file` does exactly three things:

  1. writes the instance's stored `workflow` text to `.github/workflows/<basename>`,
  2. sets `on: push` (`edit_workflow_push`),
  3. deletes every other workflow file in that directory except ones the retained
     workflow references as a reusable workflow (`extract_referenced_workflows`).

The paper additionally describes collapsing matrix dimensions and stripping
non-validation steps. The released harness does neither, so `standardize` does neither
by default: matching the artifact is what makes our numbers comparable to their
reported Pass@1. Both are available behind flags for local execution, where a five-way
Python matrix is the difference between a feasible run and an infeasible one, and both
are recorded in the returned report so the paper can state which was used.

The second half of this module distils a workflow into an ordered list of shell steps.
That is not something CI-Repair-Bench needs -- it pushes to GitHub and lets Actions do
the work -- but it is what makes an offline oracle possible on a cluster where Docker
and GitHub are both unavailable.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

REUSABLE = re.compile(r"\.github/workflows/([\w\-.\/]+(?:\.yml|\.yaml))")

# Steps that verify nothing: they publish, notify, or upload. Removing them cannot
# change a pass/fail verdict, but leaving them in can, because a missing token turns an
# upload into a red job. Only consulted when `drop_non_validation` is on.
NON_VALIDATION_ACTIONS = (
    "actions/upload-artifact",
    "actions/download-artifact",
    "actions/deploy-pages",
    "actions/upload-pages-artifact",
    "codecov/codecov-action",
    "coverallsapp/github-action",
    "softprops/action-gh-release",
    "pypa/gh-action-pypi-publish",
    "docker/build-push-action",
    "docker/login-action",
    "peter-evans/create-pull-request",
    "8398a7/action-slack",
    "slackapi/slack-github-action",
    "actions/github-script",
    "actions/stale",
)

NON_VALIDATION_NAME = re.compile(
    r"\b(publish|deploy|release|upload|notify|announce|slack|discord|"
    r"codecov|coveralls|badge|docker\s*push|changelog)\b",
    re.IGNORECASE,
)

# Actions we can emulate offline. Anything else means the workflow cannot be distilled
# into shell, and the local oracle must report that rather than guess.
KNOWN_ACTIONS = (
    "actions/checkout",
    "actions/setup-python",
    "actions/cache",
    "actions/setup-node",
    "astral-sh/setup-uv",
    "pdm-project/setup-pdm",
    "abatilo/actions-poetry",
    "snok/install-poetry",
)


@dataclass
class StandardizationReport:
    workflow_filename: str
    trigger_rewritten: bool = False
    workflows_deleted: list[str] = field(default_factory=list)
    workflows_kept: list[str] = field(default_factory=list)
    matrix_collapsed: dict[str, object] = field(default_factory=dict)
    steps_dropped: list[str] = field(default_factory=list)
    # Tests excluded because they fail independently of the repair. Recorded so the
    # count and the node ids are reportable rather than an invisible adjustment.
    tests_deselected: list[str] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def load_workflow(text: str) -> dict:
    """Parse workflow YAML.

    PyYAML resolves the bare key `on` to the boolean True (YAML 1.1's "yes" family), so
    a workflow's trigger arrives under `True`, not `"on"`. Every reader here would miss
    it silently. Normalise it once, on the way in.
    """
    data = yaml.safe_load(io.StringIO(text)) or {}
    if not isinstance(data, dict):
        raise ValueError("workflow YAML is not a mapping")
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def dump_workflow(data: dict) -> str:
    out = dict(data)
    trigger = out.pop("on", None)
    text = yaml.safe_dump(out, sort_keys=False, default_flow_style=False, width=4096)
    if trigger is None:
        return text
    # Quote the key so a round-trip through PyYAML does not turn it back into `true`.
    trigger_text = yaml.safe_dump({"on": trigger}, sort_keys=False, width=4096)
    trigger_text = trigger_text.replace("on:", '"on":', 1)
    name = out.get("name")
    if name is not None:
        head = yaml.safe_dump({"name": name}, sort_keys=False, width=4096)
        rest = text[len(head) :]
        return head + trigger_text + rest
    return trigger_text + text


def referenced_workflows(data: dict) -> set[str]:
    """Basenames of reusable workflows this one calls, which must survive the cleanup."""
    found: set[str] = set()

    def scan(value) -> None:
        if isinstance(value, str):
            found.update(os.path.basename(m) for m in REUSABLE.findall(value))
        elif isinstance(value, list):
            for item in value:
                scan(item)
        elif isinstance(value, dict):
            for item in value.values():
                scan(item)

    scan(data)
    return found


def collapse_matrix(job: dict, prefer: dict[str, str] | None = None) -> dict:
    """Reduce every matrix dimension to one value.

    A five-version Python matrix runs the same validation five times. For a
    pass/fail oracle that is five times the cost for no extra information, and on a
    local runner it is usually five times the chance of an unrelated environment
    failure. `prefer` pins a dimension to a chosen value -- the Python version named in
    the failing log, when we can read it -- and otherwise the first entry wins, which
    is the value GitHub reports first and so the one the log most likely came from.
    """
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return {}
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return {}

    chosen: dict[str, object] = {}
    for key, values in list(matrix.items()):
        if key in ("include", "exclude") or not isinstance(values, list) or not values:
            continue
        pick = values[0]
        if prefer and key in prefer:
            wanted = str(prefer[key])
            pick = next((v for v in values if str(v) == wanted), values[0])
        matrix[key] = [pick]
        chosen[key] = pick
    matrix.pop("include", None)
    matrix.pop("exclude", None)
    strategy.pop("fail-fast", None)
    return chosen


def is_non_validation(step: dict) -> bool:
    uses = str(step.get("uses") or "")
    if any(uses.startswith(action) for action in NON_VALIDATION_ACTIONS):
        return True
    if step.get("run"):
        return False  # a `run:` step is project code; never guess it away
    name = str(step.get("name") or "")
    return bool(uses and NON_VALIDATION_NAME.search(name))


def add_deselects(job: dict, deselect: list[str]) -> list[str]:
    """Append `--deselect` to every pytest invocation in a job's steps.

    Applied to the workflow rather than the repository so that nothing in the checkout
    differs between arms: the same command runs everywhere, and the exclusion is visible
    in the workflow file the run pushed rather than hidden in a conftest.
    """
    applied: list[str] = []
    if not deselect or not isinstance(job.get("steps"), list):
        return applied
    flags = " ".join(f'--deselect "{node}"' for node in deselect)
    for step in job["steps"]:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if not isinstance(run, str) or "pytest" not in run:
            continue
        lines = []
        for line in run.splitlines():
            stripped = line.strip()
            # Only a line that *invokes* pytest, not one that installs it.
            if stripped.startswith(("pytest", "python -m pytest", "py.test")) or (
                " pytest " in f" {stripped} " and not stripped.startswith("pip")
            ):
                line = f"{line.rstrip()} {flags}"
                applied.extend(deselect)
            lines.append(line)
        step["run"] = "\n".join(lines)
    return applied


def standardize(
    workflow_text: str,
    workflow_path: str,
    *,
    collapse_matrices: bool = False,
    drop_non_validation: bool = False,
    prefer_matrix: dict[str, str] | None = None,
    deselect: list[str] | None = None,
) -> tuple[str, StandardizationReport]:
    """Return the standardized workflow text and a record of what was changed."""
    data = load_workflow(workflow_text)
    report = StandardizationReport(workflow_filename=os.path.basename(workflow_path or ""))

    report.trigger_rewritten = data.get("on") != "push"
    data["on"] = "push"

    jobs = data.get("jobs")
    if isinstance(jobs, dict):
        report.jobs = list(jobs)
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if collapse_matrices:
                chosen = collapse_matrix(job, prefer_matrix)
                if chosen:
                    report.matrix_collapsed[job_name] = chosen
            for node in add_deselects(job, deselect or []):
                if node not in report.tests_deselected:
                    report.tests_deselected.append(node)
            if drop_non_validation and isinstance(job.get("steps"), list):
                kept = []
                for step in job["steps"]:
                    if isinstance(step, dict) and is_non_validation(step):
                        report.steps_dropped.append(
                            f"{job_name}: {step.get('name') or step.get('uses')}"
                        )
                    else:
                        kept.append(step)
                job["steps"] = kept

    return dump_workflow(data), report


def install_workflow(
    repo: Path,
    workflow_text: str,
    workflow_path: str,
    report: StandardizationReport,
) -> Path:
    """Write the standardized workflow into a checkout and delete the rest.

    Other workflows are removed for the same reason CI-Repair-Bench removes them: a
    push triggers every workflow in the directory, and an unrelated one going red would
    be scored as the candidate patch failing.
    """
    directory = repo / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (os.path.basename(workflow_path) or "workflow.yml")
    target.write_text(workflow_text, encoding="utf-8", newline="\n")

    keep = referenced_workflows(load_workflow(workflow_text)) | {target.name}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue
        if path.name in keep:
            report.workflows_kept.append(path.name)
            continue
        path.unlink()
        report.workflows_deleted.append(path.name)
    return target


# --------------------------------------------------------------------------------
# Distillation to shell, for the offline oracle
# --------------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    script: str
    working_directory: str = "."
    env: dict[str, str] = field(default_factory=dict)
    continue_on_error: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionPlan:
    """A workflow reduced to shell, or an explanation of why it could not be."""

    job: str
    python_version: str | None
    steps: list[Step] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    services: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return bool(self.steps) and not self.unsupported

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["runnable"] = self.runnable
        return payload


_EXPRESSION = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _resolve(value, bindings: dict[str, str]):
    """Substitute the `${{ matrix.x }}` expressions we can resolve, flag the rest."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match) -> str:
        expression = match.group(1).strip()
        if expression in bindings:
            return bindings[expression]
        if expression.startswith("env."):
            return bindings.get(expression, "")
        return match.group(0)

    return _EXPRESSION.sub(replace, value)


def distil(
    workflow_text: str,
    *,
    job_name: str | None = None,
    prefer_matrix: dict[str, str] | None = None,
) -> ExecutionPlan:
    """Turn one job of a workflow into an ordered list of shell steps.

    Only the actions in `KNOWN_ACTIONS` can be emulated offline: checkout is a no-op
    because the repository is already there, setup-python selects an interpreter, and
    caching is irrelevant to correctness. Any other `uses:` is recorded in
    `unsupported`, which makes the plan non-runnable. That is deliberate: an oracle
    that silently skipped an action would report a pass for a workflow it never ran.
    """
    data = load_workflow(workflow_text)
    jobs = data.get("jobs") or {}
    if not isinstance(jobs, dict) or not jobs:
        return ExecutionPlan(job="", python_version=None, unsupported=["workflow defines no jobs"])

    name = job_name or next(iter(jobs))
    job = jobs.get(name)
    if not isinstance(job, dict):
        return ExecutionPlan(job=name, python_version=None, unsupported=[f"job {name} is not a mapping"])

    bindings: dict[str, str] = {}
    matrix = ((job.get("strategy") or {}) if isinstance(job.get("strategy"), dict) else {}).get("matrix")
    if isinstance(matrix, dict):
        for key, values in matrix.items():
            if key in ("include", "exclude") or not isinstance(values, list) or not values:
                continue
            pick = values[0]
            if prefer_matrix and key in prefer_matrix:
                wanted = str(prefer_matrix[key])
                pick = next((v for v in values if str(v) == wanted), values[0])
            bindings[f"matrix.{key}"] = str(pick)

    workflow_env = {str(k): str(_resolve(v, bindings)) for k, v in (data.get("env") or {}).items()}
    job_env = {str(k): str(_resolve(v, bindings)) for k, v in (job.get("env") or {}).items()}
    for key, value in {**workflow_env, **job_env}.items():
        bindings[f"env.{key}"] = value

    default_dir = "."
    defaults = job.get("defaults")
    if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict):
        default_dir = str(defaults["run"].get("working-directory") or ".")

    plan = ExecutionPlan(
        job=name,
        python_version=None,
        env={**workflow_env, **job_env},
        services=sorted((job.get("services") or {}).keys()) if isinstance(job.get("services"), dict) else [],
    )
    if plan.services:
        plan.unsupported.append(
            f"job defines service containers ({', '.join(plan.services)}) that shell cannot start"
        )

    for index, raw in enumerate(job.get("steps") or []):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("name") or raw.get("uses") or f"step {index + 1}")
        uses = str(raw.get("uses") or "")

        if uses:
            action = uses.split("@", 1)[0]
            if action == "actions/setup-python":
                version = (raw.get("with") or {}).get("python-version")
                resolved = _resolve(str(version), bindings) if version is not None else None
                if resolved and "${{" not in resolved:
                    plan.python_version = resolved
                continue
            if action in ("actions/checkout", "actions/cache"):
                continue
            if action in KNOWN_ACTIONS:
                continue
            plan.unsupported.append(f"{label}: uses {uses}")
            continue

        script = raw.get("run")
        if not script:
            continue
        resolved = _resolve(str(script), bindings)
        if "${{" in resolved:
            plan.unsupported.append(f"{label}: unresolved expression in run block")
            continue
        plan.steps.append(
            Step(
                name=label,
                script=resolved,
                working_directory=str(raw.get("working-directory") or default_dir),
                env={str(k): str(_resolve(v, bindings)) for k, v in (raw.get("env") or {}).items()},
                continue_on_error=bool(raw.get("continue-on-error")),
            )
        )

    if not plan.steps:
        plan.unsupported.append("job has no shell steps to run")
    return plan


PYTHON_IN_LOG = re.compile(r"[Pp]ython[ -]?(3\.\d{1,2})")


def python_version_from_log(log: str) -> str | None:
    """The Python version the failing job ran under, when the log states one.

    Used to pin a collapsed matrix to the dimension the failure was actually observed
    on, rather than to whichever value the author happened to list first.
    """
    match = PYTHON_IN_LOG.search(log or "")
    return match.group(1) if match else None
