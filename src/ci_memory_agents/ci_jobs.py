"""Run patches through real CI from the dashboard, without blocking the page.

The execution oracle already exists (`oracle_github.run_ci`); this only lets the browser
start it and watch it. A build takes minutes, far longer than an HTTP request should
stay open, so each one runs on a background thread and the page polls for its state.

Two kinds of build:

- **Run builds.** Every run is built on its own. An arm has several runs
  (RUNS_PER_CONDITION) and each produced its own patch, so pass/fail is a property of
  the run, not of the arm; building an arm means building each of its runs.
- **Reference builds**, one pair per task: the failing commit untouched (must fail, or
  there is nothing to repair) and the failing commit with only the maintainers' golden
  patch (must pass, or no candidate can be scored fairly). These are the same builds
  `scripts/validate_instances.py` runs, on the same branches, cached in the same files.

"Clean" is the oracle's own guarantee rather than anything added here: the fork is reset
hard to `sha_fail` and `git clean -fdx`'d before the patch is applied, and the build runs
on a fresh GitHub Actions runner, so nothing from this machine can make it pass.

Jobs live in memory. If the dashboard is closed mid-build, GitHub still finishes the
build, but the outcome is not written; start it again, or use the command-line scripts.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .ci_outcome import outcome_path, read_outcome, write_outcome
from .dashboard_state import is_edited
from .importer import diff_paths
from .loader import load_task
from .oracle import baseline_path, candidate_diff, classify_instance, gold_path, run_attempted

_JOBS: dict[str, dict] = {}
_GUARD = threading.Lock()

REFERENCE_KINDS = ("gold", "baseline")


def _links(base: str, branch: str = "", commit: str = "", run_url: str = "") -> dict:
    """GitHub pages for a build, most specific first. `base` is https://github.com/owner/repo."""
    links = {}
    if run_url:
        links["run"] = run_url
    if base and branch:
        links["actions"] = f"{base}/actions?query={quote('branch:' + branch)}"
    if base and commit:
        links["commit"] = f"{base}/commit/{commit}"
    if base and branch:
        links["branch"] = f"{base}/tree/{branch}"
    return links


def _run_dir(runs_root: Path, agent: str, task_id: str, condition: str, run: str) -> Path:
    return runs_root / agent / task_id / condition / run


def _task(tasks_root: Path, task_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", task_id or "") or ".." in task_id:
        raise ValueError(f"invalid task id {task_id!r}")
    task_dir = tasks_root / task_id
    if not (task_dir / "metadata.json").exists():
        raise ValueError(f"unknown task {task_id!r}")
    return load_task(task_dir)


def _live(key: str) -> dict | None:
    with _GUARD:
        job = _JOBS.get(key)
        if job is None:
            return None
        snapshot = dict(job)
        snapshot["elapsed_s"] = int(time.time() - job["started_at"])
        return snapshot


def _stored(path: Path) -> dict | None:
    outcome = read_outcome(path)
    if outcome is None:
        return None
    # https://github.com/owner/repo/actions/runs/N -> https://github.com/owner/repo
    base = "/".join(outcome.run_url.split("/")[:5]) if outcome.run_url else ""
    return {
        "state": "done",
        "conclusion": outcome.conclusion,
        "detail": outcome.detail,
        "failed_jobs": outcome.failed_jobs,
        "branch": outcome.branch,
        "commit": outcome.commit,
        "elapsed_s": outcome.polled_seconds,
        "links": _links(base, outcome.branch, outcome.commit, outcome.run_url),
    }


def _launch(key: str, tasks_root: Path, task_id: str, label: str, out_path: Path, repos_dir: Path, make_diff) -> None:
    """Start one build on a background thread; its outcome is written to `out_path`."""
    from .oracle_github import GitHubConfig, run_ci

    task = _task(tasks_root, task_id)
    config = GitHubConfig.from_env(repos_dir)
    metadata = json.loads((task.root / "metadata.json").read_text(encoding="utf-8"))
    branch = f"{config.branch_prefix}/{task_id}/{label}"
    base = f"https://github.com/{config.owner}/{metadata['repo_name']}"
    # No links yet: the branch does not exist on GitHub until the push, and a link that
    # 404s reads as a broken build. `progress` adds each link once it resolves.
    job = {
        "state": "running",
        "phase": "Preparing a clean checkout of the failing commit and applying the patch. "
                 "GitHub links appear once the branch is pushed",
        "started_at": time.time(),
        "branch": branch,
        "links": {},
    }
    with _GUARD:
        _JOBS[key] = job

    def progress(event: str, commit: str = "", run_url: str = "") -> None:
        with _GUARD:
            if event == "pushed":
                job["commit"] = commit
                job["phase"] = "Pushed to GitHub. Waiting for GitHub Actions to start the workflow"
                job["links"] = _links(base, branch, commit)
            elif event == "running":
                job["phase"] = "GitHub Actions is running the workflow"
                job["links"] = _links(base, branch, job.get("commit", ""), run_url)

    def work() -> None:
        try:
            outcome = run_ci(config, task, make_diff(task), label, progress=progress)
            outcome.source = "github"
            write_outcome(out_path, outcome)
            update = {
                "state": "done",
                "conclusion": outcome.conclusion,
                "detail": outcome.detail,
                "failed_jobs": outcome.failed_jobs,
                "commit": outcome.commit,
                "links": _links(base, branch, outcome.commit, outcome.run_url),
            }
        except Exception as error:  # surfaced to the page, not swallowed
            update = {"state": "error", "detail": f"{type(error).__name__}: {error}"}
        with _GUARD:
            job.update(update)

    threading.Thread(target=work, name=f"ci-{task_id}-{label}", daemon=True).start()


# --- Run builds ----------------------------------------------------------------------

def eligibility(tasks_root: Path, runs_root: Path, agent: str, task_id: str, condition: str, run: str) -> tuple[bool, str]:
    """Whether this run has a patch to build, and if not, why not.

    Two different reasons, kept apart because they mean different things for the
    results: a run no agent answered is missing data, while a run whose agent answered
    without changing any file is a failed repair that needs no build to be scored.
    """
    task = _task(tasks_root, task_id)
    run_dir = _run_dir(runs_root, agent, task_id, condition, run)
    workspace = run_dir / "workspace"
    if workspace.exists() and is_edited(workspace, task.repo_before):
        return True, ""
    if run_attempted(run_dir):
        return False, "No usable patch: the agent answered but changed no files. It counts as a failed repair; there is nothing to build."
    return False, "Not run yet: no agent has answered this run, so there is no patch to build."


def status(tasks_root: Path, runs_root: Path, agent: str, task_id: str, condition: str, run: str) -> dict:
    """The live job if one exists this session, else the stored outcome, else why there is none."""
    run_dir = _run_dir(runs_root, agent, task_id, condition, run)
    live = _live(str(run_dir))
    if live is not None:
        return live
    stored = _stored(outcome_path(run_dir))
    if stored is not None:
        return stored
    eligible, reason = eligibility(tasks_root, runs_root, agent, task_id, condition, run)
    return {"state": "none", "eligible": eligible, "reason": reason}


def start(tasks_root: Path, runs_root: Path, agent: str, task_id: str, condition: str, run: str, repos_dir: Path) -> dict:
    """Start a clean CI build of one run's patch. Returns the job's initial state."""
    run_dir = _run_dir(runs_root, agent, task_id, condition, run)
    key = str(run_dir)
    current = _live(key)
    if current is not None and current["state"] == "running":
        return current
    eligible, reason = eligibility(tasks_root, runs_root, agent, task_id, condition, run)
    if not eligible:
        raise ValueError(reason)
    _launch(
        key, tasks_root, task_id, f"{condition}-{run}", outcome_path(run_dir), repos_dir,
        lambda task: candidate_diff(run_dir, task),
    )
    return status(tasks_root, runs_root, agent, task_id, condition, run)


def start_arm(tasks_root: Path, runs_root: Path, agent: str, task_id: str, condition: str, repos_dir: Path) -> dict:
    """Build every run of one task's arm that has a patch and no CI result yet.

    Runs already built keep their result (rebuild one with its own button), runs being
    built are left alone, and runs with no patch are reported with the reason instead of
    being pushed.
    """
    condition_dir = runs_root / agent / task_id / condition
    runs = sorted(p.name for p in condition_dir.glob("run_*") if p.is_dir())
    if not runs:
        raise ValueError(f"No runs for {task_id} / {condition}. Lay out the prompts first.")
    started: list[str] = []
    skipped: dict[str, str] = {}
    for run in runs:
        current = status(tasks_root, runs_root, agent, task_id, condition, run)
        if current["state"] == "running":
            skipped[run] = "already building"
        elif current["state"] == "done" and current.get("conclusion") in ("success", "failure"):
            skipped[run] = f"already built: {current['conclusion']}"
        elif current["state"] == "none" and not current.get("eligible"):
            skipped[run] = current["reason"]
        else:
            start(tasks_root, runs_root, agent, task_id, condition, run, repos_dir)
            started.append(run)
    return {"runs": runs, "started": started, "skipped": skipped}


# --- Reference builds ----------------------------------------------------------------

def _reference_path(task, kind: str) -> Path:
    return gold_path(task.root) if kind == "gold" else baseline_path(task.root)


def status_reference(tasks_root: Path, task_id: str, kind: str) -> dict:
    """One reference build's state, plus the task's verdict from both builds."""
    if kind not in REFERENCE_KINDS:
        raise ValueError(f"unknown reference build {kind!r}; expected gold or baseline")
    task = _task(tasks_root, task_id)
    gold_file = task.root / "gold_patch.diff"
    state = _live(f"{task.root}|{kind}") or _stored(_reference_path(task, kind))
    if state is None:
        if kind == "gold" and not gold_file.exists():
            state = {"state": "none", "eligible": False, "reason": "This task has no gold_patch.diff."}
        else:
            state = {"state": "none", "eligible": True, "reason": ""}
    state["kind"] = kind
    if kind == "gold" and gold_file.exists():
        state["files"] = diff_paths(gold_file.read_text(encoding="utf-8"))
    state["verdict"] = classify_instance(read_outcome(baseline_path(task.root)), read_outcome(gold_path(task.root)))
    return state


def start_reference(tasks_root: Path, task_id: str, kind: str, repos_dir: Path) -> dict:
    """Build the unpatched failing commit, or the failing commit with only the golden patch.

    The golden-patch build applies `gold_patch.diff` and nothing else: no agent output,
    no workspace, so no file the maintainers did not change can be touched.
    """
    if kind not in REFERENCE_KINDS:
        raise ValueError(f"unknown reference build {kind!r}; expected gold or baseline")
    task = _task(tasks_root, task_id)
    key = f"{task.root}|{kind}"
    current = _live(key)
    if current is not None and current["state"] == "running":
        return status_reference(tasks_root, task_id, kind)
    if kind == "gold":
        gold_file = task.root / "gold_patch.diff"
        if not gold_file.exists():
            raise ValueError("This task has no gold_patch.diff.")
        make_diff = lambda _task: gold_file.read_text(encoding="utf-8")  # noqa: E731
    else:
        make_diff = lambda _task: None  # noqa: E731  -- None means: apply nothing
    _launch(key, tasks_root, task_id, kind, _reference_path(task, kind), repos_dir, make_diff)
    return status_reference(tasks_root, task_id, kind)
