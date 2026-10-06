# CI Memory Agents Runner

Drives the whole experiment through GitHub Copilot without any copying and pasting.

## Why an extension rather than Playwright

Copilot Chat has no public HTTP API and no CLI that edits files, and browser automation
cannot reliably drive VS Code's Electron UI. VS Code does expose a supported way to call
Copilot from code: the [Language Model API](https://code.visualstudio.com/api/extension-guides/language-model)
(`vscode.lm`). It uses your existing Copilot subscription, so no API key is needed.

## Install

Copy this folder into `%USERPROFILE%\.vscode\extensions\` under a directory named
`<publisher>.<name>-<version>`, then **Developer: Reload Window**.

The version is read from `package.json` rather than hardcoded. Hardcoding it is how you
end up with two installs of different vintages side by side and no way to tell which one
VS Code loaded:

```powershell
cd D:\research\ci-memory-agents\vscode-extension
$v = (Get-Content package.json | ConvertFrom-Json).version
$dest = "$env:USERPROFILE\.vscode\extensions\ci-memory-agents.ci-memory-agents-runner-$v"

# Remove every earlier install first. Two folders whose package.json claim the same
# version is a conflict VS Code resolves silently, and not always in your favour.
Get-ChildItem "$env:USERPROFILE\.vscode\extensions" -Filter "ci-memory-agents.*" |
    Remove-Item -Recurse -Force

New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item * $dest -Recurse -Force
```

Then reload VS Code. To confirm which build is live, run **CI Memory: List Available
Copilot Models** — it logs `EXTENSION_VERSION` to the *CI Memory Agents* output channel.
`EXTENSION_VERSION` in `extension.js` and `version` in `package.json` are kept equal on
purpose; if they ever differ, the install is stale.

## Which conditions it runs

Whatever exists on disk. `run_experiment.py --control-arm` lays out a third arm,
`foreign_memory`, alongside `no_memory` and `with_memory`. Earlier versions of this
extension hardcoded the pair and silently skipped those cells — the worst kind of missing
data, because nothing reports it. Arms are now read from the task folder, so a future
fourth arm runs without an extension change.

## Use

`Ctrl+Shift+P`, then:

| Command | What it does |
|---|---|
| **CI Memory: List Available Copilot Models** | Shows which models you can use |
| **CI Memory: Run A Single Task** | Pick one task, run every condition and repeat |
| **CI Memory: Run All Experiments** | Run every pending run, unattended |

The first call asks permission to use Copilot. Approve it once.

Progress appears as a notification with a cancel button, and a full log goes to the
**CI Memory Agents** output channel. Cancelling is safe: finished runs are kept and the
next start resumes from where it stopped.

## Settings

`ciMemory.repoRoot`, `ciMemory.agent`, `ciMemory.modelFamily`, `ciMemory.delaySeconds`,
`ciMemory.maxRuns`.

Only two Copilot cells are accepted, and each must run with its own model:

| `ciMemory.agent` | `ciMemory.modelFamily` |
|---|---|
| `copilot-gpt-5.4` | `gpt-5.4` |
| `copilot-claude-fable-5.1` | `claude-fable-5.1` |

Any other agent, or a model that does not match the agent, is refused before a request is
sent, so one model's answers can never be filed under another's name.

## Starting a run from cmd or the dashboard

On the dashboard, each task has **▶ No mem / ▶ K1 / ▶ K3 / ▶ K5** buttons, in the task
list and on the run page. A button opens a command window running:

```
python scripts/run_copilot.py --agent copilot-gpt-5.4 --task-id crb_agno_129 --condition memory_k3
```

which you can also type yourself. Copilot has no command-line agent, so the script hands
the request to this extension through a
`vscode://ci-memory-agents.ci-memory-agents-runner/run?...` link. The extension runs that
task's pending runs in that arm, and the command window prints its progress until the
batch ends.

- VS Code must be open and signed in to Copilot. The first time, VS Code asks whether to
  open the link; choose **Open**.
- Only one batch runs at a time. A second request while one is running is refused, and
  its window says so.
- Progress files are written to `.ci_batches/` in the repository root.

## When something goes wrong

Every failure gets a message: a pop-up, a line in the **CI Memory Agents** output channel
with what to do, and an `agent_error.json` in the run folder that the dashboard shows on
the run. A failed run gets no `agent_meta.json`, so it stays pending and the next batch
retries it.

| Problem | What the batch does |
|---|---|
| **Out of Copilot quota** | Stops, with a button to open your Copilot usage page |
| **Rate limited** / **network error** | Waits 60 s and retries, 3 attempts; stops if it still fails |
| **Prompt too long for this model** | Checked before sending; skips that run and continues |
| **Request blocked** / **empty reply** / unexpected error | Skips that run; stops after 3 in a row |
| **Copilot access not granted** / **model not available** | Stops |
| **Cancelled** | Stops; finished runs are kept |

## What it writes per run

- `workspace/` — files the model changed
- `agent_response.md` — the raw reply, kept for auditing
- `agent_meta.json` — model id, timings, files written

A run with `agent_meta.json` is treated as finished and skipped next time. Delete that
file to redo a run.

## Limits

This calls the model directly, so it is a single-shot request rather than Copilot's
multi-step agent mode with tool use. That is a deliberate trade: both conditions get
exactly the same treatment, which is what the comparison needs. The prompt already
contains the full repository files, so no exploration step is required. Say so in the
paper: the finding is about the model with and without CI memory, not about Copilot's
agent harness.
