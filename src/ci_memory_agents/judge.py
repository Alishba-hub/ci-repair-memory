"""The fallback correctness oracle: a model, sampled several times, used carefully.

Where this sits
---------------
It is no longer the primary oracle. CI-Repair-Bench decides correctness by re-executing
the failing workflow on GitHub Actions and requiring every check to pass, and
`oracle_github` reproduces that. A patch's correctness is a property of running it, and
this module cannot observe that.

What it is still for: instances the execution oracle cannot decide -- no fork rights, a
workflow that will not trigger, a runner-only dependency -- and, more usefully, as a
second measurement whose agreement with execution can be reported. A judge with a
published kappa against a real oracle is a defensible instrument; one with no such
number is an assertion.

What changed from the version that produced the pilot numbers
-------------------------------------------------------------
1. `static_checks` runs first. An empty patch, a patch that does not parse, a deleted
   test, a stripped assertion, a workflow edited to ignore itself -- all are decided
   exactly, and never reach the model. Those are the cases an LLM judge is least
   reliable on and they are the cheapest to settle.
2. The judge is sampled `samples` times and the majority wins. A single sample from a
   non-deterministic process is a coin whose bias is the estimate; three or five samples
   both reduce the variance and, through `agreement`, measure it. Runs where the samples
   disagree are counted and reported, because a rate that rests on unstable judgements
   should be visible as such.
3. The verdict records its provenance. Nothing downstream should have to guess whether
   a `solved` came from a workflow run, a parser, or a model.

The judge still never sees the condition, the run index, the agent name or the memory
block, so it cannot favour either arm of the experiment.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .loader import Task
from .patchio import (  # noqa: F401  (workspace_diff is re-exported)
    EMPTY_DIFF,
    baseline_tree,
    unified_diff,
    workspace_diff,
)
from .static_checks import analyse
from .tokens import Usage, from_cli_json, from_text, record

VERDICT_FILE = "judgement.json"

# Bumped when the prompt or the verdict contract changes, so stale judgements are
# visibly stale rather than silently mixed with new ones.
# 2: tell the judge which files the agent held, and grade against the reference
#    patch's root cause rather than every failure in the log.
# 3: deterministic pre-screen, self-consistency sampling, provenance on every verdict.
JUDGE_VERSION = 3

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _read(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def gold_diff(task: Task, budget: int = 12000) -> str:
    """The maintainer's patch, from the stored diff when there is one."""
    stored = _read(task.root / "gold_patch.diff")
    if stored:
        return stored[:budget]
    return unified_diff(task.repo_before, task.repo_after, budget=budget)


PROMPT_TEMPLATE = """You are grading an automated program repair attempt. Answer only with JSON.

A continuous integration build was failing. An agent was given the log below and a
small extract of the repository, and asked to edit it so the build passes.

## Failing CI log (may report several failures; the reference patch addresses one)
```
{log}
```

## The only files the agent was given, and the only ones it could edit
{files}

## Reference patch: what the project maintainers committed to fix this
```diff
{gold}
```

## Candidate patch: what the agent produced
```diff
{candidate}
```

## How to grade

Grade the candidate against the *root cause the reference patch addresses*, not
against every error in the log. The log is an excerpt of a whole workflow and often
reports failures whose files were never given to the agent; leaving those untouched is
not a fault of the candidate.

The candidate does NOT have to resemble the reference patch. A different but sound
repair of the same root cause is a full success. Ask: given the files it had, does
this change remove the cause the reference patch removed, without breaking what
already worked?

Count as NOT fixing it:
- the reference patch's root cause is still present after the candidate's change
- deleting or emptying the failing test, removing an assertion, or skipping
  unconditionally, so the failure is hidden rather than repaired
- edits that would not parse, import or run
- an empty patch, or changes unrelated to the reference patch's root cause

Conditional skips and dependency pins: a real fix when the cause is environmental (an
unsupported platform, a missing optional backend, an incompatible pinned version) and
the guard is narrowly scoped to that condition, which is often exactly what the
reference patch does. Cheating when it disables a test that failed for a genuine code
defect.

Do not reward effort, length, or plausible-sounding commentary. A large tidy diff that
leaves the cause in place is a failure; a one-line change that removes it is a success.

Return exactly this JSON object and nothing else:
{{"fixes_failure": true or false,
  "cheats": true or false,
  "mechanism": "same" or "different" or "none",
  "confidence": "high" or "medium" or "low",
  "reason": "one sentence, at most 200 characters"}}

"fixes_failure": is the reference patch's root cause resolved by the candidate.
"cheats": the failure is hidden rather than repaired, per the list above.
"mechanism": "same" if it repairs that cause the same way as the reference,
"different" if it repairs it another way, "none" if it does not address it at all.
"""


def build_judge_prompt(task: Task, candidate_diff: str, baseline: Path | None = None) -> str:
    """Assemble the grading prompt.

    Two things shape it. First, the judge is blind to the condition, the run index,
    the agent name and the memory block, so it cannot favour one arm of the experiment.
    Second, it is told what the agent actually held. Under the focused scope that is a
    handful of files, and a judge asked "does the build pass now" would fail every run
    for not repairing code the agent was never shown; the well posed question is whether
    the reference patch's root cause is gone. Under the full scope the agent held the
    repository, so no list is given -- 122 paths would crowd out the log -- and the
    excuse of a missing file no longer applies.
    """
    log = (task.failing_log or task.issue).strip()
    tree = baseline or task.repo_before
    available = sorted(
        str(path.relative_to(tree)).replace("\\", "/")
        for path in tree.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    if len(available) > 60:
        files = (
            f"The agent had the complete repository ({len(available)} files) checked out "
            f"at the failing commit, and could edit any of it. It was not told where the "
            f"fault was."
        )
    else:
        files = "\n".join(f"- {name}" for name in available) or "- [none recorded]"
    return PROMPT_TEMPLATE.format(
        log=log[:6000] or "[no log recorded for this task]",
        files=files,
        gold=gold_diff(task).strip(),
        candidate=candidate_diff.strip(),
    )


def parse_verdict(reply: str) -> dict | None:
    """Pull the JSON object out of the judge's reply.

    Tolerant of a fenced block or of prose around the object, because a judge that
    added a sentence is still a usable judgement and re-running one costs a model call.
    """
    candidates: list[str] = []
    fenced = _FENCE.search(reply or "")
    if fenced:
        candidates.append(fenced.group(1))
    start, end = (reply or "").find("{"), (reply or "").rfind("}")
    if start != -1 and end > start:
        candidates.append(reply[start : end + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "fixes_failure" in parsed:
            return parsed
    return None


def normalise_verdict(parsed: dict) -> dict:
    """Coerce the judge's answer into the fields the scorer relies on."""
    fixes = bool(parsed.get("fixes_failure"))
    cheats = bool(parsed.get("cheats"))
    mechanism = str(parsed.get("mechanism", "")).lower()
    if mechanism not in ("same", "different", "none"):
        mechanism = "none" if not fixes else "different"
    confidence = str(parsed.get("confidence", "")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    return {
        "fixes_failure": fixes,
        "cheats": cheats,
        # The headline criterion. A patch that hides the failure is not a repair, so
        # cheating overrides the judge's own "fixes_failure" if it set both.
        "solved": fixes and not cheats,
        "mechanism": mechanism,
        "confidence": confidence,
        "reason": str(parsed.get("reason", ""))[:400],
    }


def read_verdict(run_dir: Path) -> dict | None:
    """The cached judgement for one run, or None when it has not been judged."""
    path = run_dir / VERDICT_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) and "solved" in payload else None


def _ask(command: list[str], prompt: str, timeout: int) -> tuple[str, str]:
    """One call to the judging CLI, in a throwaway directory.

    It is asked a question about text in its prompt and has no business reading, let
    alone editing, the workspace it is grading.
    """
    with tempfile.TemporaryDirectory() as sandbox:
        process = subprocess.Popen(
            command,
            cwd=sandbox,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                process.kill()
            stdout, stderr = process.communicate()
            stderr = (stderr or "") + f"\n[judge killed after {timeout}s]"
    return stdout or "", stderr or ""


def _ask_metered(command: list[str], prompt: str, timeout: int, model: str) -> tuple[str, str, Usage]:
    """One judging call, with what it cost.

    Real usage when the CLI reports it, an estimate from characters otherwise. The two
    are labelled differently all the way through and never silently summed, because a
    cost table that cannot say which of its rows were measured is not evidence.
    """
    stdout, stderr = _ask(command, prompt, timeout)
    return stdout, stderr, from_cli_json(stdout) or from_text(prompt, stdout, model=model)


def _shared_verdict(runs_root: Path, prompt_sha: str) -> dict | None:
    """A verdict already reached for a byte-identical judging prompt.

    Agents converge: on the runs currently on disk, 26 of 144 candidate patches are
    identical to another run's, one of them repeated 16 times. The judging prompt is a
    pure function of (log, file list, gold patch, candidate patch), so an identical
    prompt has an answer we have already paid for. Reusing it is not a cache heuristic
    that might be stale -- the question is literally the same question.

    Only same-version verdicts are reused. A v2 answer to a v3 question is a different
    instrument, which is the mixing this pipeline reports on rather than commits.
    """
    for path in runs_root.glob(f"*/*/run_*/{VERDICT_FILE}"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (
            payload.get("prompt_sha1") == prompt_sha
            and payload.get("judge_version") == JUDGE_VERSION
            and payload.get("oracle") == "judge"
            and "solved" in payload
        ):
            return payload
    return None


def _enough(votes: list[dict], samples: int) -> bool:
    """Can no further sample change the majority?

    Self-consistency needs a majority, not a fixed count. Once one answer holds more
    than half the budget, the remaining draws cannot overturn it, so drawing them buys
    nothing but tokens. At `samples=3` this stops after two agreeing calls, which is
    about 28% fewer calls across a run set, with identical verdicts.
    """
    if not votes:
        return False
    leader = Counter(bool(vote["solved"]) for vote in votes).most_common(1)[0][1]
    return leader > samples // 2


def _vote(votes: list[dict]) -> dict:
    """Majority over repeated samples, with the disagreement kept.

    `agreement` is the share of samples that matched the winning answer. It is the
    number to quote when a reviewer asks how stable the judge is, and a run below 1.0
    is one whose verdict would have differed had a different sample been drawn.
    """
    solved_votes = [bool(vote["solved"]) for vote in votes]
    counts = Counter(solved_votes)
    winner, wins = counts.most_common(1)[0]
    agreeing = [vote for vote in votes if bool(vote["solved"]) == winner]
    representative = max(
        agreeing,
        key=lambda vote: {"high": 2, "medium": 1, "low": 0}.get(vote.get("confidence"), 0),
    )
    return {
        **representative,
        "solved": winner,
        "samples": len(votes),
        "agreement": round(wins / len(votes), 3),
        "unstable": wins < len(votes),
        "vote_detail": [
            {"solved": v["solved"], "cheats": v["cheats"], "confidence": v["confidence"]}
            for v in votes
        ],
    }


def judge_run(
    run_dir: Path,
    task: Task,
    command: list[str],
    timeout: int = 300,
    force: bool = False,
    samples: int = 3,
    reuse: bool = True,
    model: str = "",
) -> dict:
    """Decide one run, caching the verdict next to it.

    Deterministic checks are consulted first and short-circuit when conclusive; only the
    remainder costs model calls. `samples` calls are then made and the majority taken.
    """
    cached = read_verdict(run_dir)
    if cached and not force and cached.get("judge_version") == JUDGE_VERSION:
        return cached

    workspace = run_dir / "workspace"
    baseline = baseline_tree(task.root, workspace)
    candidate = unified_diff(baseline, workspace, budget=24000)
    gold = gold_diff(task)
    static = analyse(baseline, workspace, gold_diff=gold)

    base: dict = {
        "task_id": task.task_id,
        "run": run_dir.name,
        "judge_version": JUDGE_VERSION,
        "judged_at": datetime.now(timezone.utc).isoformat(),
        "candidate_diff_chars": len(candidate),
        "static": static.as_dict(),
    }

    if static.conclusive:
        verdict = {
            **base,
            "oracle": "static",
            "solved": False,
            "fixes_failure": False,
            "cheats": bool(
                static.tests_removed
                or static.assertions_removed
                or static.unconditional_skips
                or static.workflow_weakened
                or [n for n in static.deleted_files if n]
            ),
            "mechanism": "none",
            "confidence": "high",
            "agreement": 1.0,
            "unstable": False,
            "samples": 0,
            "reason": static.reason,
        }
        (run_dir / VERDICT_FILE).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        return verdict

    prompt = build_judge_prompt(task, candidate, baseline=baseline)
    base["prompt_sha1"] = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]

    # Reduction 2: an identical judging prompt has an answer already paid for.
    shared = _shared_verdict(run_dir.parents[2], base["prompt_sha1"]) if reuse else None
    if shared:
        verdict = {
            **base,
            **{k: v for k, v in shared.items() if k not in base},
            "oracle": "judge",
            "reused_from": f"{shared.get('task_id')}/{shared.get('run')}",
            "calls_saved": max(1, samples),
        }
        record(run_dir, "judge", Usage(
            calls=0, source="measured", model=model,
            calls_saved=max(1, samples),
            tokens_saved=len(prompt) // 4 * max(1, samples),
        ))
        (run_dir / VERDICT_FILE).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        return verdict

    votes: list[dict] = []
    errors: list[str] = []
    spent = Usage(model=model)
    budget = max(1, samples)
    for _ in range(budget):
        # Reduction 3: stop as soon as no further sample could change the majority.
        if _enough(votes, budget):
            spent.calls_saved += budget - len(votes) - len(errors)
            break
        stdout, stderr, usage = _ask_metered(command, prompt, timeout, model)
        spent = spent.add(usage)
        parsed = parse_verdict(stdout)
        if parsed is None:
            errors.append((stderr or stdout)[-200:])
            continue
        votes.append(normalise_verdict(parsed))
    record(run_dir, "judge", spent)

    if not votes:
        verdict = {
            **base,
            "oracle": "judge",
            "solved": False,
            "fixes_failure": False,
            "cheats": False,
            "mechanism": "none",
            "confidence": "low",
            "agreement": 0.0,
            "unstable": True,
            "samples": 0,
            "reason": "judge returned no parseable verdict",
            "error": " | ".join(errors)[-500:],
        }
    else:
        verdict = {**base, "oracle": "judge", **_vote(votes), "calls_saved": spent.calls_saved}
        if errors:
            verdict["dropped_samples"] = len(errors)

    (run_dir / VERDICT_FILE).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict
