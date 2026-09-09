const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

const EXTENSION_VERSION = "0.3.5";

// The two-arm comparison. Kept only as the ordering key for interleave(): which arms
// actually exist is read from disk by conditionsOn(), because run_experiment.py can lay
// out arms this list does not know about, and a hardcoded list once silently skipped
// one -- the cells were created, the extension never ran them, and nothing said so.
// Ordered so a partial batch is a balanced sample: control first, then the memory arms
// by increasing K. `conditionsOn` still runs anything else it finds on disk, so this
// list controls order, never membership -- membership is what a hardcoded list got
// wrong before, when cells were created and then silently never run.
const KNOWN_CONDITIONS = [
  "no_memory",
  "memory_k1",
  "memory_k3",
  "memory_k5",
  "with_memory",
  "foreign_memory",
];
const FILE_HEADER = /^===\s*(.+?)\s*===$/;

function config() {
  const settings = vscode.workspace.getConfiguration("ciMemory");
  return {
    repoRoot: settings.get("repoRoot"),
    agent: settings.get("agent"),
    modelFamily: settings.get("modelFamily"),
    delaySeconds: settings.get("delaySeconds"),
    maxRuns: settings.get("maxRuns"),
    allowRouterModel: settings.get("allowRouterModel"),
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
/**
 * Which experimental arms this task actually has on disk.
 *
 * Read rather than assumed. A hardcoded list once meant cells were laid out and then
 * quietly never run, which is the worst kind of missing data because nothing reports it.
 * Anything that is a directory and holds run folders is an arm.
 */
function conditionsOn(taskDir) {
  if (!fs.existsSync(taskDir)) return [];
  const found = fs
    .readdirSync(taskDir)
    .filter((name) => fs.statSync(path.join(taskDir, name)).isDirectory());
  // Known arms first, in experiment order, then anything else so a future arm still runs.
  return [
    ...KNOWN_CONDITIONS.filter((c) => found.includes(c)),
    ...found.filter((c) => !KNOWN_CONDITIONS.includes(c)),
  ];
}

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
    for (const condition of conditionsOn(path.join(agentRoot, taskId))) {
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
 * task, then condition puts each task's arms next to each other, so even a small
 * maxRuns yields complete matched sets rather than one arm of many tasks.
 */
function interleave(pending) {
  return [...pending].sort((a, b) => {
    if (a.run !== b.run) return a.run.localeCompare(b.run);
    if (a.taskId !== b.taskId) return a.taskId.localeCompare(b.taskId);
    return a.condition.localeCompare(b.condition);
  });
}

async function pickModel(family, allowRouter) {
  // Ask for everything first, so a wrong `modelFamily` can be told apart from not being
  // signed in. Selecting straight on the family returns an empty list either way, and
  // "sign in to Copilot" is the wrong thing to tell someone who is signed in and simply
  // typed a family string that does not exist.
  const all = await vscode.lm.selectChatModels({ vendor: "copilot" });
  if (!all.length) {
    throw new Error(
      "No Copilot model available at all. Sign in to GitHub Copilot in VS Code, then retry."
    );
  }
  if (!family) {
    throw new Error(
      "ciMemory.modelFamily is not set. Pin one model before collecting results: " +
        "comparing conditions across different models would confound the experiment. " +
        "Available: " +
        [...new Set(all.map((m) => m.family))].join(", ")
    );
  }
  const matched = all.filter((m) => m.family === family);
  if (!matched.length) {
    throw new Error(
      `No Copilot model with family "${family}". You are signed in and ${all.length} ` +
        `model(s) are available, so this is a settings typo rather than an auth problem. ` +
        `Set ciMemory.modelFamily to exactly one of: ` +
        [...new Set(all.map((m) => m.family))].join(", ")
    );
  }
  const chosen = matched[0];
  if (chosen.id === "auto" && !allowRouter) {
    throw new Error(
      `Model family "${family}" resolves to id "auto", Copilot's router: it may pick a ` +
        `different underlying model per request, which makes the model an uncontrolled ` +
        `variable *inside* a condition. If that is the only capable model available, set ` +
        `ciMemory.allowRouterModel to true to proceed. Every run then records the model ` +
        `it actually got, and score_runs.py reports whether it varied -- a router that ` +
        `stayed on one model throughout is harmless, and one that did not is at least ` +
        `visible instead of silent.`
    );
  }
  return chosen;
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
/**
 * Split a model reply into { path: contents }.
 *
 * The prompt asks for `=== path ===` before each file, and when a model obeys, that is
 * unambiguous and nothing else is consulted. Some models answer with prose and a fenced
 * block instead, naming the file in a sentence -- a perfectly reasonable reply that the
 * header-only parser scored as "NO FILES PARSED", discarding complete submissions over
 * formatting. `known` and `sizes` enable the fallback; without them behaviour is
 * unchanged. Mirrors parse_files() in src/ci_memory_agents/response_parser.py.
 */
function parseFiles(reply, known, sizes) {
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

  if (Object.keys(files).length || !known || !known.length) return files;
  return parseFenced(lines, known, sizes || {});
}

// How much of the original a fenced block must cover before it counts as the whole file.
// Models routinely quote a short excerpt of the change as well as the finished file, and
// writing a 116-character excerpt over a 5,000-character test reads as a deliberate
// deletion rather than a parse failure -- a failure the harness would have invented.
const MIN_WHOLE_FILE_RATIO = 0.5;

function parseFenced(lines, known, sizes) {
  const paths = [...known].sort((a, b) => b.length - a.length);
  const files = {};
  let seen = null;

  for (let i = 0; i < lines.length; i += 1) {
    if (!/^```[A-Za-z0-9_+-]*\s*$/.test(lines[i].trim())) {
      for (const path of paths) {
        const base = path.split("/").pop();
        if (lines[i].includes(path) || lines[i].includes(base)) {
          seen = path;
          break;
        }
      }
      continue;
    }

    const body = [];
    i += 1;
    while (i < lines.length && !/^```/.test(lines[i].trim())) {
      body.push(lines[i]);
      i += 1;
    }

    const target = seen || (known.length === 1 ? known[0] : null);
    if (!target || !body.length) continue;
    // A model opening with ```python sometimes repeats the language as the first body
    // line. It is never valid source and would break the file it lands in.
    if (["python", "py", "yaml", "yml", "json", "toml", "text"].includes(body[0].trim())) {
      body.shift();
    }
    // Keep the largest block for a path: an excerpt often precedes the complete file.
    const candidate = body.join("\n");
    if (candidate.length > (files[target] || "").length) files[target] = candidate;
  }

  for (const path of Object.keys(files)) {
    if (sizes[path] && files[path].length < sizes[path] * MIN_WHOLE_FILE_RATIO) {
      delete files[path];
    }
  }
  return files;
}

/** The paths a task offers and their original sizes, for the fallback above. */
function repoFiles(repoRoot, taskId) {
  const before = path.join(repoRoot, "tasks", taskId, "repo_before");
  const known = [];
  const sizes = {};
  if (!fs.existsSync(before)) return { known, sizes };
  const walk = (dir, prefix) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === ".git") continue;
      const full = path.join(dir, entry.name);
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(full, rel);
      else {
        known.push(rel);
        sizes[rel] = fs.readFileSync(full, "utf8").length;
      }
    }
  };
  walk(before, "");
  known.sort();
  return { known, sizes };
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
    model = await pickModel(settings.modelFamily, settings.allowRouterModel);
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
          const { known, sizes } = repoFiles(settings.repoRoot, item.taskId);
          const files = parseFiles(reply, known, sizes);
          const written = writeFiles(path.join(item.runDir, "workspace"), files);

          fs.writeFileSync(path.join(item.runDir, "agent_response.md"), reply, "utf8");
          fs.writeFileSync(
            path.join(item.runDir, "agent_meta.json"),
            JSON.stringify(
              {
                // `model` is the field the Python side reads to attribute a run to an
                // LLM. It is the family rather than the id, because the id carries a
                // build suffix that changes under us while the family is what the
                // experiment pins and what `design.AGENTS` names.
                model: model.family,
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
