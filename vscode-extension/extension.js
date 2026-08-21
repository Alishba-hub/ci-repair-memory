const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

const EXTENSION_VERSION = "0.2.0";
const CONDITIONS = ["no_memory", "with_memory"];
const FILE_HEADER = /^===\s*(.+?)\s*===$/;

function config() {
  const settings = vscode.workspace.getConfiguration("ciMemory");
  return {
    repoRoot: settings.get("repoRoot"),
    agent: settings.get("agent"),
    modelFamily: settings.get("modelFamily"),
    delaySeconds: settings.get("delaySeconds"),
    maxRuns: settings.get("maxRuns"),
  };
}

function readTree(root) {
  const files = {};
  if (!fs.existsSync(root)) return files;
  const walk = (dir, prefix) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === ".git") continue;
      const full = path.join(dir, entry.name);
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(full, rel);
      else files[rel] = fs.readFileSync(full, "utf8");
    }
  };
  walk(root, "");
  return files;
}

/** True once a workspace differs from the untouched snapshot. */
function isEdited(workspace, repoBefore) {
  const a = readTree(workspace);
  const b = readTree(repoBefore);
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) if (a[key] !== b[key]) return true;
  return false;
}

/**
 * Every run folder that still needs an agent response.
 *
 * A workspace that already differs from repo_before is skipped even without
 * agent_meta.json: that is a run someone did by hand, and overwriting it would
 * destroy real data.
 */
function pendingRuns(repoRoot, agent, taskFilter) {
  const agentRoot = path.join(repoRoot, "runs", agent);
  if (!fs.existsSync(agentRoot)) {
    throw new Error(`No runs folder at ${agentRoot}. Generate prompts first.`);
  }
  const pending = [];
  let protectedCount = 0;
  for (const taskId of fs.readdirSync(agentRoot).sort()) {
    if (taskFilter && taskId !== taskFilter) continue;
    const repoBefore = path.join(repoRoot, "tasks", taskId, "repo_before");
    for (const condition of CONDITIONS) {
      const conditionDir = path.join(agentRoot, taskId, condition);
      if (!fs.existsSync(conditionDir)) continue;
      for (const run of fs.readdirSync(conditionDir).sort()) {
        const runDir = path.join(conditionDir, run);
        if (!fs.existsSync(path.join(runDir, "prompt.md"))) continue;
        if (fs.existsSync(path.join(runDir, "agent_meta.json"))) continue;
        if (fs.existsSync(repoBefore) && isEdited(path.join(runDir, "workspace"), repoBefore)) {
          protectedCount += 1;
          continue;
        }
        pending.push({ taskId, condition, run, runDir });
      }
    }
  }
  const ordered = interleave(pending);
  ordered.protectedCount = protectedCount;
  return ordered;
}

/**
 * Order runs so that any prefix is a balanced sample.
 *
 * Folder order would spend a small maxRuns budget entirely on one task in one
 * condition, which cannot answer the research question. Sorting by run index, then
 * task, then condition puts each task's two conditions next to each other, so even
 * maxRuns=2 yields one complete matched pair.
 */
function interleave(pending) {
  return [...pending].sort((a, b) => {
    if (a.run !== b.run) return a.run.localeCompare(b.run);
    if (a.taskId !== b.taskId) return a.taskId.localeCompare(b.taskId);
    return a.condition.localeCompare(b.condition);
  });
}

async function pickModel(family) {
  const selector = family ? { vendor: "copilot", family } : { vendor: "copilot" };
  const models = await vscode.lm.selectChatModels(selector);
  if (!models.length) {
    throw new Error(
      "No Copilot model available. Sign in to GitHub Copilot in VS Code and try again."
    );
  }
  return models[0];
}

async function askModel(model, prompt, token) {
  const messages = [vscode.LanguageModelChatMessage.User(prompt)];
  const response = await model.sendRequest(messages, {}, token);
  let text = "";
  for await (const chunk of response.text) {
    text += chunk;
  }
  return text;
}

/**
 * Split an agent reply into files.
 *
 * The prompt asks for each changed file to be preceded by `=== path ===`.
 * Models often wrap the body in a markdown fence anyway, so fences are stripped.
 */
function parseFiles(reply) {
  const files = {};
  const lines = reply.split(/\r?\n/);
  let current = null;
  let buffer = [];

  const flush = () => {
    if (current) files[current] = stripFences(buffer).join("\n");
    buffer = [];
  };

  for (const line of lines) {
    const header = line.match(FILE_HEADER);
    if (header) {
      flush();
      current = header[1].replace(/^[`'"]|[`'"]$/g, "").trim();
      continue;
    }
    if (current) buffer.push(line);
  }
  flush();
  return files;
}

function stripFences(lines) {
  let body = [...lines];
  while (body.length && !body[0].trim()) body.shift();
  while (body.length && !body[body.length - 1].trim()) body.pop();
  if (body.length && /^```/.test(body[0].trim())) {
    body.shift();
    if (body.length && body[body.length - 1].trim() === "```") body.pop();
  }
  return body;
}

function writeFiles(workspace, files) {
  const written = [];
  for (const [relative, content] of Object.entries(files)) {
    const safe = relative.replace(/\\/g, "/").replace(/^\.\//, "");
    if (safe.includes("..") || path.isAbsolute(safe)) continue;
    const target = path.join(workspace, safe);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content.endsWith("\n") ? content : content + "\n", "utf8");
    written.push(safe);
  }
  return written;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function runBatch(taskFilter) {
  const settings = config();
  let model;
  try {
    model = await pickModel(settings.modelFamily);
  } catch (error) {
    vscode.window.showErrorMessage(error.message);
    return;
  }

  let pending;
  try {
    pending = pendingRuns(settings.repoRoot, settings.agent, taskFilter);
  } catch (error) {
    vscode.window.showErrorMessage(error.message);
    return;
  }
  if (!pending.length) {
    vscode.window.showInformationMessage("Every run already has an agent response.");
    return;
  }
  const protectedCount = pending.protectedCount || 0;
  if (settings.maxRuns > 0) pending = pending.slice(0, settings.maxRuns);

  const output = vscode.window.createOutputChannel("CI Memory Agents");
  output.show(true);
  output.appendLine(`Extension v${EXTENSION_VERSION} (balanced run order)`);
  output.appendLine(`Model: ${model.id} (${model.vendor}/${model.family})`);
  output.appendLine(`Runs to do: ${pending.length}`);
  if (protectedCount) {
    output.appendLine(`Skipping ${protectedCount} run(s) already edited by hand.`);
  }
  output.appendLine("");

  let completed = 0;
  let failed = 0;

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "CI Memory experiment",
      cancellable: true,
    },
    async (progress, token) => {
      for (const [index, item] of pending.entries()) {
        if (token.isCancellationRequested) {
          output.appendLine("\nCancelled.");
          break;
        }
        const label = `${item.taskId} ${item.condition} ${item.run}`;
        progress.report({
          message: `${index + 1}/${pending.length}  ${label}`,
          increment: 100 / pending.length,
        });

        const prompt = fs.readFileSync(path.join(item.runDir, "prompt.md"), "utf8");
        const started = Date.now();
        try {
          const reply = await askModel(model, prompt, token);
          const files = parseFiles(reply);
          const written = writeFiles(path.join(item.runDir, "workspace"), files);

          fs.writeFileSync(path.join(item.runDir, "agent_response.md"), reply, "utf8");
          fs.writeFileSync(
            path.join(item.runDir, "agent_meta.json"),
            JSON.stringify(
              {
                model_id: model.id,
                model_family: model.family,
                model_vendor: model.vendor,
                prompt_chars: prompt.length,
                reply_chars: reply.length,
                files_written: written,
                duration_ms: Date.now() - started,
                finished_at: new Date().toISOString(),
              },
              null,
              2
            ),
            "utf8"
          );
          completed += 1;
          output.appendLine(
            `[${index + 1}/${pending.length}] ok   ${label} -> ${
              written.length ? written.join(", ") : "NO FILES PARSED"
            }`
          );
        } catch (error) {
          failed += 1;
          output.appendLine(`[${index + 1}/${pending.length}] FAIL ${label}: ${error.message}`);
          if (/quota|rate|429|limit/i.test(error.message)) {
            output.appendLine("Rate limited. Waiting 60s before continuing.");
            await sleep(60000);
          }
        }
        if (settings.delaySeconds > 0) await sleep(settings.delaySeconds * 1000);
      }
    }
  );

  output.appendLine(`\nDone. ${completed} succeeded, ${failed} failed.`);
  vscode.window.showInformationMessage(
    `CI Memory: ${completed} runs finished, ${failed} failed. Score them in the dashboard.`
  );
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("ciMemory.runAll", () => runBatch(null)),

    vscode.commands.registerCommand("ciMemory.runOne", async () => {
      const settings = config();
      const agentRoot = path.join(settings.repoRoot, "runs", settings.agent);
      if (!fs.existsSync(agentRoot)) {
        vscode.window.showErrorMessage(`No runs folder at ${agentRoot}`);
        return;
      }
      const choice = await vscode.window.showQuickPick(fs.readdirSync(agentRoot).sort(), {
        placeHolder: "Pick a task to run",
      });
      if (choice) await runBatch(choice);
    }),

    vscode.commands.registerCommand("ciMemory.listModels", async () => {
      const models = await vscode.lm.selectChatModels({ vendor: "copilot" });
      const output = vscode.window.createOutputChannel("CI Memory Agents");
      output.show(true);
      if (!models.length) {
        output.appendLine("No Copilot models. Sign in to GitHub Copilot first.");
        return;
      }
      output.appendLine("Available Copilot models (use the family value in settings):\n");
      for (const model of models) {
        output.appendLine(
          `  family: ${model.family}\n    id: ${model.id}\n    max input tokens: ${model.maxInputTokens}\n`
        );
      }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate, parseFiles, stripFences };
