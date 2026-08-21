"""Execution-free correctness judging for agent repairs.

Why this exists
---------------
`normalized_match` in `evaluator.py` asks whether the agent reproduced the
maintainer's commit text. That is not the research question. A worked example from
`crb_agentscope_342`, where the CI failure is Milvus Lite being unavailable on the
Windows runner:

    maintainer   if os.name == "nt": self.skipTest("Milvus Lite ... on Windows.")
    agent        @unittest.skipUnless(_milvus_lite_available(), "... not installed")

Both resolve the failure and the second is arguably the better guard, yet they share
no text, so every textual metric scores the agent zero. Measured over the 40 runs of
the two completed tasks, patch similarity to gold sits between 0.07 and 0.45 with no
separation between correct and incorrect repairs, so no threshold on it is defensible.

The judge instead answers the question the experiment actually asks: does this patch
resolve the CI failure? It sees the failing log, the maintainer's patch as a reference,
and the candidate patch. It never sees the condition, the run index, or the memory
block, so it cannot favour `with_memory` by knowing which arm it is scoring.

Judgements are cached per run in `judgement.json`, so re-scoring the whole experiment
costs nothing and a judgement is reproducible from the file that produced it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .loader import Task

VERDICT_FILE = "judgement.json"

# Bumped when the prompt or the verdict contract changes, so stale judgements are
# visibly stale rather than silently mixed with new ones.
# 2: tell the judge which files the agent held, and grade against the reference
#    patch's root cause rather than every failure in the log.
JUDGE_VERSION = 2

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _read(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _walk(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def workspace_diff(repo_before: Path, workspace: Path, budget: int = 24000) -> str:
    """Unified diff of what the agent actually changed on disk.

    The agent's reply text is not used: it may describe edits it never made, and only
    the files are evaluated. Truncation is announced in-band so the judge can see that
    it is reading an excerpt rather than a complete patch.
    """
    names = sorted(_walk(repo_before) | _walk(workspace))
    blocks: list[str] = []
    used = 0
    for name in names:
        old = _read(repo_before / name)
        new = _read(workspace / name)
        if old == new:
            continue
        lines = list(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
                n=3,
            )
        )
        text = "".join(lines)
        if len(text) > 6000:
            text = text[:6000] + f"\n[... {len(text) - 6000} more characters in this file's diff]\n"
        if used + len(text) > budget:
            blocks.append(f"--- a/{name}\n+++ b/{name}\n[diff omitted: over the prompt budget]\n")
            continue
        used += len(text)
        blocks.append(text)
    return "".join(blocks) or "[the agent changed no files]"


def gold_diff(task: Task, budget: int = 12000) -> str:
    """The maintainer's patch, from the stored diff when there is one."""
    stored = _read(task.root / "gold_patch.diff")
    if stored:
        return stored[:budget]
    return workspace_diff(task.repo_before, task.repo_after, budget)


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


def build_judge_prompt(task: Task, candidate_diff: str) -> str:
    """Assemble the grading prompt.

    Two things shape it. First, the judge is blind to the condition, the run index,
    the agent name and the memory block, so it cannot favour one arm of the experiment.
    Second, it is told which files the agent actually held: `repo_before` carries only
    the gold files, while the log reports the whole workflow's failures, so a judge
    asked "does the build pass now" would fail every run for not repairing code it was
    never shown. The well posed question is whether the reference patch's root cause is
    gone.
    """
    log = (task.failing_log or task.issue).strip()
    available = sorted(
        str(path.relative_to(task.repo_before)).replace("\\", "/")
        for path in task.repo_before.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    return PROMPT_TEMPLATE.format(
        log=log[:6000] or "[no log recorded for this task]",
        files="\n".join(f"- {name}" for name in available) or "- [none recorded]",
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


def judge_run(
    run_dir: Path,
    task: Task,
    command: list[str],
    timeout: int = 300,
    force: bool = False,
) -> dict:
    """Judge one run, caching the verdict next to it.

    The judge runs in a throwaway working directory: it is asked a question about text
    in its prompt and has no business reading, let alone editing, the workspace it is
    grading.
    """
    cached = read_verdict(run_dir)
    if cached and not force and cached.get("judge_version") == JUDGE_VERSION:
        return cached

    candidate = workspace_diff(task.repo_before, run_dir / "workspace")
    prompt = build_judge_prompt(task, candidate)

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

    parsed = parse_verdict(stdout or "")
    verdict: dict = {
        "task_id": task.task_id,
        "run": run_dir.name,
        "judge_version": JUDGE_VERSION,
        "judged_at": datetime.now(timezone.utc).isoformat(),
        "prompt_sha1": hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12],
        "candidate_diff_chars": len(candidate),
        "raw_reply_chars": len(stdout or ""),
    }
    if parsed is None:
        verdict.update(
            {
                "solved": False,
                "fixes_failure": False,
                "cheats": False,
                "mechanism": "none",
                "confidence": "low",
                "reason": "judge returned no parseable verdict",
                "error": (stderr or stdout or "")[-500:],
            }
        )
    else:
        verdict.update(normalise_verdict(parsed))

    (run_dir / VERDICT_FILE).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict
