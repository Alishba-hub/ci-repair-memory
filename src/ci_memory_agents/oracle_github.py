"""The execution oracle: re-run the real CI workflow and read its conclusion.

This is a re-implementation of CI-Repair-Bench's own evaluation loop, taken from
`benchmark.py` / `benchmark_functions.py` in their replication package, so that a
number produced here means the same thing as the Pass@1 they report:

    clone the fork at sha_fail -> reset --hard -> clean -fdx -> branch
    -> install the instance's workflow with `on: push`, delete the others
    -> apply the candidate patch
    -> commit, force-push
    -> poll /commits/{sha}/check-runs until every run completes
    -> success iff every conclusion is "success"

Two things are done differently, both deliberately.

CI-Repair-Bench applies the patch and pushes without first proving the checkout was
red. We push the unpatched commit once per instance and cache that outcome, because a
"pass" is only meaningful against a baseline that failed: an instance whose workflow is
green before any repair would otherwise contribute a free success to both arms. This is
the equivalence check their paper describes in Section 3.1 and their code omits.

Their poller collapses anything that is neither all-success nor any-failure into
"error", which loses the difference between a workflow that was cancelled, one that
timed out, and one whose jobs were all skipped. The raw conclusions are kept here, and
the collapse to pass/fail happens once, visibly, in `_conclude`.

Requires a GitHub token with `repo` scope and forks of the benchmark repositories under
one account. It costs Actions minutes, which are free for public repositories, and no
model tokens at all -- which is the point: the oracle is the cheap part of this
experiment, and replacing it with a language model traded reliability for nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import workflow_std
from .ci_outcome import CIOutcome, outcome_path, read_outcome, write_outcome
from .patchio import apply_patch, check_applicable

API = "https://api.github.com"

# Conclusions GitHub can report for a check run. Only "success" counts as a pass;
# "skipped" and "neutral" are kept apart from failures because a workflow whose jobs all
# skipped never validated anything and must not be scored as a repair.
PASSING = frozenset({"success"})
FAILING = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})


class OracleError(RuntimeError):
    """The oracle could not reach a verdict, as against reaching a negative one."""


def _load_secrets_file() -> None:
    """Fill in credentials from `.secrets.json` for anything the environment lacks.

    Read here rather than in one entry point because the scripts are meant to be usable
    on their own: a token stored by `run.py login` should work when `score_runs.py` is
    called directly, otherwise the stored credential is a feature of one command rather
    than of the harness. The environment always wins, so a CI job or a one-off shell can
    still override without touching the file.
    """
    path = Path(__file__).resolve().parents[2] / ".secrets.json"
    if not path.exists():
        return
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key in ("GITHUB_TOKEN", "GITHUB_USERNAME", "BENCHMARK_OWNER"):
        if not os.environ.get(key) and stored.get(key):
            os.environ[key] = str(stored[key])


@dataclass
class GitHubConfig:
    """Where the forks live and who is pushing to them."""

    owner: str
    username: str
    token: str
    repos_dir: Path
    poll_interval: int = 30
    poll_timeout: int = 3600
    branch_prefix: str = "cirepair"

    @classmethod
    def from_env(cls, repos_dir: Path, owner: str | None = None) -> "GitHubConfig":
        _load_secrets_file()
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        username = os.environ.get("GITHUB_USERNAME", "").strip()
        owner = (owner or os.environ.get("BENCHMARK_OWNER", "") or username).strip()
        if not token:
            raise OracleError("GITHUB_TOKEN is not set; the execution oracle cannot push")
        if not owner:
            raise OracleError("set BENCHMARK_OWNER (or GITHUB_USERNAME) to the account holding the forks")
        return cls(owner=owner, username=username or owner, token=token, repos_dir=repos_dir)


def _request(config: GitHubConfig, path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {config.token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "ci-memory-agents/0.2")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise OracleError(f"GitHub {method} {url} -> {error.code}: {detail}") from error


def ensure_fork(config: GitHubConfig, upstream_owner: str, repo: str) -> str:
    """Make sure `config.owner/repo` exists, forking it if it does not.

    Returns the fork's full name. Forking is asynchronous on GitHub's side, so this
    waits for the repository to become readable before handing it back.
    """
    try:
        _request(config, f"/repos/{config.owner}/{repo}")
        return f"{config.owner}/{repo}"
    except OracleError:
        pass
    _request(config, f"/repos/{upstream_owner}/{repo}/forks", method="POST", body={})
    for _ in range(30):
        time.sleep(5)
        try:
            _request(config, f"/repos/{config.owner}/{repo}")
            return f"{config.owner}/{repo}"
        except OracleError:
            continue
    raise OracleError(f"fork of {upstream_owner}/{repo} did not appear under {config.owner}")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        raise OracleError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result


# One lock per repository. The clone is reused across instances -- deliberately, since
# re-cloning CPython for every run would dominate the wall clock -- but that makes it
# shared mutable state, and `git` refuses to run two operations in one working tree at
# once ("index.lock: File exists"). Locking per repo keeps parallelism where it pays,
# across different projects, and serialises only what git cannot do concurrently.
_REPO_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def repo_lock(repo_name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(repo_name, threading.Lock())


def checkout(config: GitHubConfig, repo_name: str, sha: str, branch: str) -> Path:
    """A clean checkout of the fork at the failing commit, on its own branch.

    Follows CI-Repair-Bench's `get_repo`: a shallow clone reused across instances, a
    hard reset to the target commit, and `clean -fdx` so nothing an earlier instance
    left on disk can leak into this one.
    """
    config.repos_dir.mkdir(parents=True, exist_ok=True)
    path = config.repos_dir / repo_name
    remote = f"https://{config.username}:{config.token}@github.com/{config.owner}/{repo_name}.git"

    if not (path / ".git").exists():
        shutil.rmtree(path, ignore_errors=True)
        result = subprocess.run(
            ["git", "clone", "--filter=blob:none", remote, str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise OracleError(f"clone of {config.owner}/{repo_name} failed: {result.stderr.strip()[:300]}")

    _git(path, "remote", "set-url", "origin", remote)
    _git(path, "fetch", "--depth", "1", "origin", sha)
    _git(path, "reset", "--hard", sha)
    _git(path, "clean", "-fdx")
    _git(path, "checkout", "-B", branch)
    return path


def push(config: GitHubConfig, repo: Path, branch: str, message: str) -> str:
    """Commit everything in the working tree and force-push. Returns the commit sha."""
    _git(repo, "-c", "user.email=ci-memory-agents@local", "-c", "user.name=ci-memory-agents",
         "add", "-A")
    committed = subprocess.run(
        ["git", "-c", "user.email=ci-memory-agents@local", "-c", "user.name=ci-memory-agents",
         "commit", "--allow-empty", "-m", message],
        cwd=repo, capture_output=True, text=True,
    )
    if committed.returncode != 0 and "nothing to commit" not in committed.stdout:
        raise OracleError(f"commit failed: {committed.stderr.strip()[:300]}")
    _git(repo, "push", "--force", "--set-upstream", "origin", branch)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _conclude(check_runs: list[dict]) -> tuple[str, list[str], list[str], str]:
    """Collapse GitHub's per-check conclusions into one workflow-level verdict.

    A repair is successful if and only if every check run concluded "success", which is
    CI-Repair-Bench's rule. The two departures from their code are that a run which is
    still in progress is reported as such rather than as an error, and that a workflow
    whose checks all skipped is `inconclusive`, not a pass -- nothing was validated, so
    there is no evidence either way.
    """
    if not check_runs:
        return "inconclusive", [], [], "no check runs were created for this commit"
    statuses = [run.get("status") for run in check_runs]
    conclusions = [run.get("conclusion") for run in check_runs]
    if not all(status == "completed" for status in statuses):
        return "pending", [c for c in conclusions if c], [], "still running"

    failed = [run.get("name", "?") for run in check_runs if run.get("conclusion") in FAILING]
    if failed:
        return "failure", conclusions, failed, ""
    if all(conclusion in PASSING for conclusion in conclusions):
        return "success", conclusions, [], ""
    if all(conclusion in ("skipped", "neutral") for conclusion in conclusions):
        return "inconclusive", conclusions, [], "every check was skipped; nothing was validated"
    return "inconclusive", conclusions, [], f"unhandled conclusions: {sorted(set(conclusions))}"


def poll(config: GitHubConfig, repo_name: str, commit: str) -> CIOutcome:
    """Wait for the pushed commit's checks to finish and report the outcome."""
    deadline = time.time() + config.poll_timeout
    started = time.time()
    last = CIOutcome(conclusion="inconclusive", commit=commit, detail="never polled")
    # GitHub needs a moment to create the check runs after a push; polling instantly
    # returns an empty list, which would otherwise read as "no workflow triggered".
    time.sleep(min(20, config.poll_interval))
    while time.time() < deadline:
        payload = _request(config, f"/repos/{config.owner}/{repo_name}/commits/{commit}/check-runs")
        runs = payload.get("check_runs") or []
        conclusion, conclusions, failed, detail = _conclude(runs)
        run_url = ""
        if runs:
            html = runs[0].get("html_url") or ""
            run_url = "/".join(html.split("/")[:-2]) if html else ""
        last = CIOutcome(
            conclusion=conclusion if conclusion != "pending" else "inconclusive",
            raw_conclusions=[c for c in conclusions if c],
            run_url=run_url,
            commit=commit,
            failed_jobs=failed,
            detail=detail,
            polled_seconds=int(time.time() - started),
        )
        if conclusion != "pending":
            return last
        time.sleep(config.poll_interval)
    last.detail = f"still running after {config.poll_timeout}s"
    return last


def run_ci(
    config: GitHubConfig,
    task,
    candidate_diff: str | None,
    label: str,
    *,
    collapse_matrices: bool = False,
    drop_non_validation: bool = False,
) -> CIOutcome:
    """Re-execute the instance's workflow with `candidate_diff` applied.

    Pass `candidate_diff=None` for the baseline: the failing commit with nothing but the
    workflow standardization, which must come back red for the instance to be usable.
    """
    metadata = json.loads((task.root / "metadata.json").read_text(encoding="utf-8"))
    repo_name = metadata["repo_name"]
    sha = metadata["sha_fail"]
    branch = f"{config.branch_prefix}/{metadata['task_id']}/{label}"

    # Fork before cloning. `validate_instances` did this and `run_ci` did not, so
    # executing candidates without validating first failed on the first task with
    # "Repository not found" -- correct, since nothing had ever created the fork.
    # `ensure_fork` returns immediately when it already exists, so this is cheap.
    ensure_fork(config, metadata.get("repo_owner", ""), repo_name)

    # The lock spans checkout through push, not checkout alone. Two threads sharing one
    # clone could otherwise interleave: thread A resets the tree while thread B is
    # between writing its patch and committing, and B pushes A's changes under B's
    # branch. That is a wrong answer rather than a crash, which is worse.
    with repo_lock(repo_name):
        return _run_ci_locked(
            config, task, candidate_diff, label, metadata, repo_name, sha, branch,
            collapse_matrices=collapse_matrices, drop_non_validation=drop_non_validation,
        )


def _run_ci_locked(
    config: GitHubConfig,
    task,
    candidate_diff: str | None,
    label: str,
    metadata: dict,
    repo_name: str,
    sha: str,
    branch: str,
    *,
    collapse_matrices: bool = False,
    drop_non_validation: bool = False,
) -> CIOutcome:
    checkout_path = checkout(config, repo_name, sha, branch)

    prefer = {}
    version = workflow_std.python_version_from_log(task.failing_log)
    if version:
        prefer["python-version"] = version
    text, report = workflow_std.standardize(
        task.workflow or metadata.get("workflow", ""),
        metadata.get("workflow_path", ""),
        # Read from the instance first, exactly like `deselect_tests`. A matrix collapsed
        # for candidates but not for the baseline would mean the two were graded by
        # different workflows, and the difference between them would be the workflow.
        # The caller's flag remains as an override for a one-off experiment.
        collapse_matrices=bool(metadata.get("collapse_matrices", collapse_matrices)),
        drop_non_validation=bool(metadata.get("drop_non_validation", drop_non_validation)),
        prefer_matrix=prefer,
        # Read from the instance, so the same exclusions apply to the baseline, the gold
        # patch and every candidate. Anything arm-specific here would be the effect.
        deselect=metadata.get("deselect_tests") or [],
    )
    workflow_std.install_workflow(checkout_path, text, metadata.get("workflow_path", ""), report)

    if candidate_diff is not None:
        applicability = check_applicable(candidate_diff, checkout_path)
        if not applicability.applies:
            return CIOutcome(
                conclusion="failure",
                branch=branch,
                detail=f"patch does not apply: {applicability.reason}",
            )
        failure = apply_patch(candidate_diff, checkout_path)
        if failure:
            return CIOutcome(conclusion="failure", branch=branch, detail=f"patch apply failed: {failure}")

    commit = push(config, checkout_path, branch, f"{metadata['task_id']} {label}")
    outcome = poll(config, repo_name, commit)
    outcome.branch = branch
    return outcome
