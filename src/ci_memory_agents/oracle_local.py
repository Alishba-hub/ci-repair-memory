"""An offline execution oracle, for when pushing to GitHub Actions is not an option.

CI-Repair-Bench never needs this: it pushes to a fork and lets GitHub run the workflow.
That is the right default and `oracle_github` implements it. This module exists for the
two cases where it is unavailable -- a compute cluster with no outbound push rights, and
a batch large enough that Actions minutes become the bottleneck -- and for the Alliance
clusters in particular, where Docker is not permitted and Apptainer is.

The honest limitation, which belongs in the paper rather than in a footnote: a workflow
distilled to shell is not the workflow. Service containers, marketplace actions beyond
the handful in `workflow_std.KNOWN_ACTIONS`, and runner-provided software are all gone.
So this oracle reports `inconclusive` for anything it cannot faithfully run, and never
guesses. Instances it can run should still be reported separately from those scored on
GitHub, and the agreement between the two backends on the overlap is worth a table.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import workflow_std
from .ci_outcome import CIOutcome
from .patchio import apply_patch, check_applicable
from .workflow_std import ExecutionPlan

STEP_MARKER = "@@@CI_MEMORY_AGENTS_STEP@@@"


@dataclass
class LocalConfig:
    """How to execute a distilled workflow.

    `backend` is "docker", "apptainer" or "host". The first two isolate the run, which
    matters because CI steps install packages and write outside the repository; "host"
    exists for debugging and should not produce numbers for a paper.

    `pip_index` points pip at a date-pinned index. CI-Repair-Bench's own standardized
    workflows do this for 61 instances via a `pypi-wayback` service container, and it is
    the difference between a dependency-resolution failure that reproduces and one that
    depends on what PyPI happens to hold today. Set it to
    `http://<host>/<commit date>` when running an instance whose failure is a dependency
    or installation error.
    """

    backend: str = "docker"
    image: str = "python:{python}-bookworm"
    default_python: str = "3.11"
    timeout: int = 1800
    network: bool = True
    pip_index: str | None = None
    workdir: Path | None = None

    def image_for(self, plan: ExecutionPlan) -> str:
        return self.image.format(python=plan.python_version or self.default_python)


def _script(plan: ExecutionPlan, config: LocalConfig) -> str:
    """One shell script for the whole job.

    Every step must run in the same shell session: a step that creates a virtualenv and
    a later step that activates it are only meaningful together, and a step-per-container
    design silently breaks half the workflows in this benchmark. Failure is attributed to
    a step by the markers rather than by running them separately.
    """
    lines = ["#!/usr/bin/env bash", "set -u", "cd /workspace"]
    for key, value in plan.env.items():
        lines.append(f"export {shlex.quote(key)}={shlex.quote(str(value))}")
    if config.pip_index:
        lines.append(f"export PIP_INDEX_URL={shlex.quote(config.pip_index)}")
        lines.append(f"export UV_INDEX_URL={shlex.quote(config.pip_index)}")
    lines.append("export PIP_DISABLE_PIP_VERSION_CHECK=1")
    lines.append("export CI=true")
    lines.append("export GITHUB_ACTIONS=true")

    for index, step in enumerate(plan.steps):
        lines.append(f'echo "{STEP_MARKER} BEGIN {index}"')
        lines.append("(")
        lines.append("set -e")
        if step.working_directory and step.working_directory != ".":
            lines.append(f"cd {shlex.quote(step.working_directory)}")
        for key, value in step.env.items():
            lines.append(f"export {shlex.quote(key)}={shlex.quote(str(value))}")
        lines.append(step.script)
        lines.append(")")
        lines.append("status=$?")
        lines.append(f'echo "{STEP_MARKER} END {index} $status"')
        if not step.continue_on_error:
            # The workflow stops at the first red step, as GitHub Actions does. Running
            # on would report failures caused by the first one and misattribute them.
            lines.append('if [ "$status" -ne 0 ]; then exit "$status"; fi')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def _command(config: LocalConfig, plan: ExecutionPlan, repo: Path, script: Path) -> list[str]:
    image = config.image_for(plan)
    if config.backend == "docker":
        command = [
            "docker", "run", "--rm",
            "-v", f"{repo.resolve()}:/workspace",
            "-v", f"{script.resolve()}:/tmp/run.sh:ro",
            "-w", "/workspace",
        ]
        if not config.network:
            command += ["--network", "none"]
        return command + [image, "bash", "/tmp/run.sh"]

    if config.backend == "apptainer":
        # Alliance clusters allow Apptainer and not Docker. `--writable-tmpfs` gives the
        # steps somewhere to install into without a writable image.
        command = [
            "apptainer", "exec", "--writable-tmpfs", "--containall",
            "--bind", f"{repo.resolve()}:/workspace",
            "--bind", f"{script.resolve()}:/tmp/run.sh",
            "--pwd", "/workspace",
        ]
        if config.network:
            command += ["--net", "--network", "bridge"]
        return command + [f"docker://{image}", "bash", "/tmp/run.sh"]

    if config.backend == "host":
        return ["bash", str(script)]

    raise ValueError(f"unknown backend {config.backend!r}")


def _attribute(output: str, plan: ExecutionPlan) -> tuple[list[dict], list[str]]:
    """Read the step markers back out of the combined output."""
    results: list[dict] = []
    failed: list[str] = []
    for line in output.splitlines():
        if not line.startswith(STEP_MARKER) or " END " not in line:
            continue
        parts = line.split()
        try:
            index, status = int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            continue
        if index >= len(plan.steps):
            continue
        name = plan.steps[index].name
        results.append({"step": name, "exit_code": status})
        if status != 0:
            failed.append(name)
    return results, failed


def run_plan(repo: Path, plan: ExecutionPlan, config: LocalConfig) -> CIOutcome:
    """Execute a distilled workflow against a checkout and report pass or fail."""
    if not plan.runnable:
        return CIOutcome(
            conclusion="inconclusive",
            source=f"local:{config.backend}",
            detail="; ".join(plan.unsupported)[:400] or "nothing to run",
        )
    if config.backend in ("docker", "apptainer") and shutil.which(config.backend) is None:
        return CIOutcome(
            conclusion="inconclusive",
            source=f"local:{config.backend}",
            detail=f"{config.backend} is not on PATH",
        )

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
    )
    handle.write(_script(plan, config))
    handle.close()
    script = Path(handle.name)
    started = time.time()
    try:
        process = subprocess.run(
            _command(config, plan, repo, script),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout,
        )
        output = (process.stdout or "") + "\n" + (process.stderr or "")
        code = process.returncode
        timed_out = False
    except subprocess.TimeoutExpired as expired:
        output = ((expired.stdout or b"").decode("utf-8", "replace")
                  + (expired.stderr or b"").decode("utf-8", "replace")
                  if isinstance(expired.stdout, bytes) else str(expired.stdout or ""))
        code, timed_out = -1, True
    except OSError as error:
        return CIOutcome(
            conclusion="inconclusive",
            source=f"local:{config.backend}",
            detail=f"could not start {config.backend}: {error}",
        )
    finally:
        script.unlink(missing_ok=True)

    steps, failed = _attribute(output, plan)
    elapsed = int(time.time() - started)

    if timed_out:
        return CIOutcome(
            conclusion="inconclusive",
            source=f"local:{config.backend}",
            steps=steps,
            failed_jobs=failed,
            detail=f"workflow exceeded {config.timeout}s",
            polled_seconds=elapsed,
        )
    if not steps:
        # The container never produced a marker, so no step ever started: the image or
        # the mount is wrong, which is a harness fault and not a failing repair.
        return CIOutcome(
            conclusion="inconclusive",
            source=f"local:{config.backend}",
            detail=f"no step ran (exit {code}): {output.strip()[-300:]}",
            polled_seconds=elapsed,
        )
    return CIOutcome(
        conclusion="success" if code == 0 else "failure",
        source=f"local:{config.backend}",
        steps=steps,
        failed_jobs=failed,
        detail="" if code == 0 else f"step failed: {failed[0] if failed else 'unknown'}",
        polled_seconds=elapsed,
    )


def clone_at(repo_url: str, sha: str, destination: Path) -> Path:
    """A checkout of `repo_url` at `sha`, without the full history."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (destination / ".git").exists():
        shutil.rmtree(destination, ignore_errors=True)
    subprocess.run(
        ["git", "init", "-q", str(destination)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", repo_url],
        cwd=destination, check=True, capture_output=True,
    )
    fetch = subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", sha],
        cwd=destination, capture_output=True, text=True,
    )
    if fetch.returncode != 0:
        raise RuntimeError(f"fetch of {sha[:8]} from {repo_url} failed: {fetch.stderr.strip()[:200]}")
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=destination, check=True, capture_output=True)
    return destination


def run_ci(
    task,
    metadata: dict,
    candidate_diff: str | None,
    config: LocalConfig,
    *,
    upstream_owner: str | None = None,
) -> CIOutcome:
    """Clone, standardize, patch and run one instance offline.

    `candidate_diff=None` runs the failing commit untouched, which is how an instance is
    shown to be red before any repair is scored against it.
    """
    owner = upstream_owner or metadata.get("repo_owner") or ""
    repo_name = metadata["repo_name"]
    root = config.workdir or Path(tempfile.gettempdir()) / "ci-memory-agents-local"
    checkout = root / f"{repo_name}_{metadata['sha_fail'][:8]}"

    try:
        clone_at(f"https://github.com/{owner}/{repo_name}.git", metadata["sha_fail"], checkout)
    except Exception as error:
        return CIOutcome(
            conclusion="inconclusive",
            source=f"local:{config.backend}",
            detail=f"could not check out {owner}/{repo_name}@{metadata['sha_fail'][:8]}: {error}",
        )

    prefer = {}
    version = workflow_std.python_version_from_log(task.failing_log)
    if version:
        prefer["python-version"] = version

    text, report = workflow_std.standardize(
        task.workflow or metadata.get("workflow", ""),
        metadata.get("workflow_path", ""),
        collapse_matrices=True,
        drop_non_validation=True,
        prefer_matrix=prefer,
    )
    workflow_std.install_workflow(checkout, text, metadata.get("workflow_path", ""), report)

    if candidate_diff is not None:
        applicability = check_applicable(candidate_diff, checkout)
        if not applicability.applies:
            return CIOutcome(
                conclusion="failure",
                source=f"local:{config.backend}",
                detail=f"patch does not apply: {applicability.reason}",
            )
        failure = apply_patch(candidate_diff, checkout)
        if failure:
            return CIOutcome(
                conclusion="failure",
                source=f"local:{config.backend}",
                detail=f"patch apply failed: {failure}",
            )

    plan = workflow_std.distil(text, prefer_matrix=prefer)
    if config.pip_index is None and metadata.get("commit_date"):
        # Nothing is assumed here; the caller opts into a pinned index. This only makes
        # the date available for building one.
        os.environ.setdefault("CI_MEMORY_AGENTS_COMMIT_DATE", metadata["commit_date"][:10])
    return run_plan(checkout, plan, config)
