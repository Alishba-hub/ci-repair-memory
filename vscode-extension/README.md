# CI Memory Agents Runner

Drives the whole experiment through GitHub Copilot without any copying and pasting.

## Why an extension rather than Playwright

Copilot Chat has no public HTTP API and no CLI that edits files, and browser automation
cannot reliably drive VS Code's Electron UI. VS Code does expose a supported way to call
Copilot from code: the [Language Model API](https://code.visualstudio.com/api/extension-guides/language-model)
(`vscode.lm`). It uses your existing Copilot subscription, so no API key is needed.

## Install

Already installed to `%USERPROFILE%\.vscode\extensions\`. Restart VS Code to load it.

To reinstall after editing:

```powershell
$t = "$env:USERPROFILE\.vscode\extensions\ci-memory-agents.ci-memory-agents-runner-0.1.0"
Remove-Item $t -Recurse -Force
New-Item -ItemType Directory -Force $t
Copy-Item "D:\research\ci-memory-agents\vscode-extension\*" $t -Recurse -Force
```

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
`ciMemory.maxRuns`. Set `modelFamily` to pin one model, otherwise the first available is
used. **Pin it before collecting real results** — comparing conditions across different
models would confound the experiment.

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
