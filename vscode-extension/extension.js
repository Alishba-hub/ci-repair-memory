const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

const EXTENSION_VERSION = "0.4.1";

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

// The Copilot cells this runner fills, and the one model family each must be run with.
// A mismatch -- the gpt-5.4 folder filled while modelFamily says something else -- would
// file one model's answers under another's name, and nothing downstream could detect it.
const ALLOWED_AGENTS = {
  "copilot-claude-fable-5.1": "claude-fable-5.1",
  "copilot-gpt-5.4": "gpt-5.4",
};

function checkAgent(agent, family) {
  const allowed = Object.entries(ALLOWED_AGENTS)
    .map(([name, model]) => `${name} (model ${model})`)
    .join(", ");
  const expected = ALLOWED_AGENTS[agent];
  if (!expected) {
    throw new Error(
      `ciMemory.agent is "${agent}". This runner only fills the Copilot cells ${allowed}. ` +
        `Set ciMemory.agent to one of them.`
    );
  }
  if (family !== expected) {
    throw new Error(
      `ciMemory.agent "${agent}" must run with model family "${expected}", but ` +
        `ciMemory.modelFamily is "${family || "(empty)"}". Set it to "${expected}" so this ` +
        `model's answers are not filed under another model's name.`
    );
  }
}

// What can go wrong with a request, what to tell the person running the batch, and what
// the batch does about it. `stop` ends the batch: when the quota is gone every later run
// would fail the same way, and recording each as a failed attempt would be invented data.
// `retry` waits and tries the same run again. A failed run gets agent_error.json but no
// agent_meta.json, so it stays pending and the next batch picks it up.
const COPILOT_USAGE_URL = "https://github.com/settings/copilot";
const MAX_ATTEMPTS = 3;
const RETRY_WAIT_S = 60;
const STREAK_LIMIT = 3;

const PROBLEMS = {
  cancelled: {
    title: "Cancelled",
    advice: "Finished runs are kept; the next batch resumes where this one stopped.",
  },
  no_permission: {
    title: "Copilot access not granted",
    advice:
      "VS Code has not allowed this extension to use Copilot. Run the command again and " +
      "choose Allow, or grant it under Accounts > Manage Language Model Access.",
    stop: true,
  },
  not_found: {
    title: "Model not available",
    advice:
      "The pinned model is not offered to this account any more. Run 'CI Memory: List " +
      "Available Copilot Models'. Do not switch models mid-study without recording it.",
    stop: true,
  },
  quota: {
    title: "Out of Copilot quota",
    advice:
      "Your Copilot requests or tokens for this period are used up. The batch stopped so " +
      "the remaining runs are not wasted as failures. Check your usage, wait for the reset " +
      "or add requests, then run again: finished runs are kept.",
    stop: true,
    link: COPILOT_USAGE_URL,
    linkLabel: "Open Copilot usage",
  },
  rate_limit: {
    title: "Rate limited",
    advice:
      `Copilot kept refusing requests after ${MAX_ATTEMPTS} attempts. Wait a few minutes, ` +
      "raise ciMemory.delaySeconds, then run again.",
    retry: true,
  },
  network: {
    title: "Network error",
    advice: `Could not reach Copilot after ${MAX_ATTEMPTS} attempts. Check the connection and run again.`,
    retry: true,
  },
  too_long: {
    title: "Prompt too long for this model",
    advice:
      "The prompt exceeds the model's input limit, so it cannot run on this model without " +
      "changing the prompt, which would change the experiment. It is skipped and stays " +
      "pending; report it as excluded.",
  },
  blocked: {
    title: "Request blocked by Copilot",
    advice: "Copilot refused this prompt (content filter or policy). It is skipped and stays pending.",
  },
  empty_reply: {
    title: "Empty reply",
    advice: "The model answered with nothing, often a silent limit. The run stays pending.",
  },
  unknown: {
    title: "Unexpected error",
    advice: "See agent_error.json in the run folder. The run stays pending.",
  },
};
for (const [kind, problem] of Object.entries(PROBLEMS)) problem.kind = kind;

function errorText(error) {
  return String((error && error.message) || error || "unknown error");
}

/** Map an error from vscode.lm (or from the checks below) to one entry of PROBLEMS. */
function classifyError(error) {
  const code = String((error && error.code) || "");
  const cause = error && error.cause ? String(error.cause.message || error.cause) : "";
  const text = `${code} ${errorText(error)} ${cause}`;
  const has = (pattern) => pattern.test(text);

  if ((error && error.name === "Canceled") || has(/\bcancel+ed\b/i)) return PROBLEMS.cancelled;
  if (code === "EmptyReply") return PROBLEMS.empty_reply;
  if (code === "PromptTooLong") return PROBLEMS.too_long;
  if (code === "NoPermissions" || has(/no ?permissions?|not (been )?(allowed|authori[sz]ed)|consent/i))
    return PROBLEMS.no_permission;
  if (code === "NotFound") return PROBLEMS.not_found;
  if (has(/quota|premium requests?|allowance|usage limit|monthly limit|billing|out of (credits|tokens)/i))
    return PROBLEMS.quota;
  if (has(/context.{0,10}(length|window)|too (long|large)|token limit|max(imum)?[ _]?(input[ _]?)?tokens|input limit/i))
    return PROBLEMS.too_long;
  if (has(/\b429\b|rate.?limit|too many requests|throttl/i)) return PROBLEMS.rate_limit;
  if (code === "Blocked" || has(/\bblocked\b|content filter|responsible ai/i)) return PROBLEMS.blocked;
  if (has(/ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|network|fetch failed|socket hang up|offline|\b50[234]\b/i))
    return PROBLEMS.network;
  return PROBLEMS.unknown;
}

/** Refuse a prompt the model cannot take before spending a request on it. */
async function checkFits(model, prompt, token) {
  if (!model.maxInputTokens || typeof model.countTokens !== "function") return;
  let used;
  try {
    used = await model.countTokens(prompt, token);
  } catch (error) {
    return; // Counting is best-effort; the request itself still reports an oversize prompt.
  }
  if (used > model.maxInputTokens) {
    throw Object.assign(
      new Error(`The prompt is ${used} tokens and ${model.family} accepts at most ${model.maxInputTokens}.`),
      { code: "PromptTooLong" }
    );
  }
}

function recordError(runDir, problem, error, model) {
  fs.writeFileSync(
    path.join(runDir, "agent_error.json"),
    JSON.stringify(
      {
        kind: problem.kind,
        title: problem.title,
        message: errorText(error),
        advice: problem.advice,
        model: model.family,
        model_id: model.id,
        at: new Date().toISOString(),
        extension_version: EXTENSION_VERSION,
      },
      null,
      2
    ),
    "utf8"
  );
}

function clearError(runDir) {
  const file = path.join(runDir, "agent_error.json");
  if (fs.existsSync(file)) fs.unlinkSync(file);
}

/**
 * Progress for a batch started from outside VS Code (scripts/run_copilot.py, the
 * dashboard's run buttons), written to <repoRoot>/.ci_batches/<request>.json so the
 * command window that asked can print it. A batch started from the command palette has
 * no request id, and this does nothing.
 */
function batchStatus(repoRoot, request, fields) {
  if (!request || !/^[A-Za-z0-9_-]{1,64}$/.test(request)) return { log() {}, set() {} };
  const file = path.join(repoRoot, ".ci_batches", `${request}.json`);
  const data = { request, state: "running", log: [], ...fields };
  const flush = () => {
    data.updated_at = new Date().toISOString();
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, JSON.stringify(data, null, 2), "utf8");
    } catch (error) {
      // Progress reporting must never take a batch down with it.
    }
  };
  flush();
  return {
    log(line) {
      data.log.push(line);
      flush();
    },
    set(update) {
      Object.assign(data, update);
      flush();
    },
  };
}

async function sleepCancellable(ms, token) {
  const until = Date.now() + ms;
  while (Date.now() < until && !token.isCancellationRequested) {
    await new Promise((resolve) => setTimeout(resolve, Math.min(1000, until - Date.now())));
  }
}

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

function pendingRuns(repoRoot, agent, taskFilter, conditionFilter) {
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
      if (conditionFilter && condition !== conditionFilter) continue;
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

// One batch at a time. Two batches over the same runs folder would both pick the same
// pending runs and write two answers into one workspace.
let batchRunning = false;

async function runBatch(taskFilter, options = {}) {
  const settings = config();
  if (options.agent) {
    // A link names a cell, and the cell brings its own model: the request cannot end up
    // combined with whatever model happens to be in settings.
    settings.agent = options.agent;
    settings.modelFamily = ALLOWED_AGENTS[options.agent] || settings.modelFamily;
  }
  const status = batchStatus(settings.repoRoot, options.request, {
    agent: settings.agent,
    task: taskFilter,
    condition: options.condition || null,
  });
  const fail = (message, state = "error") => {
    status.log(message);
    status.set({ state, message });
    vscode.window.showErrorMessage(message);
  };

  if (batchRunning) {
    fail(
      "Another CI Memory batch is already running in VS Code. Wait for it to finish, then start this one again.",
      "rejected"
    );
    return;
  }
  batchRunning = true;
  try {
    await runBatchLocked(settings, taskFilter, options, status, fail);
  } catch (error) {
    fail(`CI Memory batch crashed: ${errorText(error)}`);
  } finally {
    batchRunning = false;
  }
}

async function runBatchLocked(settings, taskFilter, options, status, fail) {
  let model;
  try {
    checkAgent(settings.agent, settings.modelFamily);
    model = await pickModel(settings.modelFamily, settings.allowRouterModel);
  } catch (error) {
    fail(error.message);
    return;
  }

  let pending;
  try {
    pending = pendingRuns(settings.repoRoot, settings.agent, taskFilter, options.condition);
  } catch (error) {
    fail(error.message);
    return;
  }
  if (!pending.length) {
    const message = "Every run already has an agent response.";
    status.log(message);
    status.set({ state: "done", total: 0, completed: 0, failed: 0, message });
    vscode.window.showInformationMessage(message);
    return;
  }
  const protectedCount = pending.protectedCount || 0;
  if (settings.maxRuns > 0) pending = pending.slice(0, settings.maxRuns);
  status.set({ total: pending.length, completed: 0, failed: 0 });

  // Every line goes to the output channel and, for a batch started from a command
  // window, to its progress file as well.
  const channel = vscode.window.createOutputChannel("CI Memory Agents");
  const output = {
    appendLine: (line) => {
      channel.appendLine(line);
      status.log(line);
    },
    show: (preserveFocus) => channel.show(preserveFocus),
  };
  output.show(true);
  if (taskFilter) output.appendLine(`Task: ${taskFilter}${options.condition ? `, arm ${options.condition}` : ""}`);
  output.appendLine(`Extension v${EXTENSION_VERSION} (balanced run order)`);
  output.appendLine(`Model: ${model.id} (${model.vendor}/${model.family})`);
  output.appendLine(`Runs to do: ${pending.length}`);
  if (protectedCount) {
    output.appendLine(`Skipping ${protectedCount} run(s) already edited by hand.`);
  }
  output.appendLine("");

  let completed = 0;
  let failed = 0;
  const failures = {}; // problem kind -> count, for the closing summary
  const warned = new Set(); // kinds already shown as a pop-up this batch
  let streak = { kind: null, count: 0 }; // consecutive failures of one kind
  let stopped = null; // the problem that ended the batch early, if any

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "CI Memory experiment",
      cancellable: true,
    },
    async (progress, token) => {
      for (const [index, item] of pending.entries()) {
        if (token.isCancellationRequested) {
          stopped = PROBLEMS.cancelled;
          break;
        }
        const tag = `[${index + 1}/${pending.length}]`;
        const label = `${item.taskId} ${item.condition} ${item.run}`;
        progress.report({
          message: `${index + 1}/${pending.length}  ${label}`,
          increment: 100 / pending.length,
        });

        const prompt = fs.readFileSync(path.join(item.runDir, "prompt.md"), "utf8");
        for (let attempt = 1; ; attempt += 1) {
          const started = Date.now();
          try {
            await checkFits(model, prompt, token);
            const reply = await askModel(model, prompt, token);
            if (!reply.trim()) {
              throw Object.assign(new Error("The model returned an empty reply."), { code: "EmptyReply" });
            }
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
            clearError(item.runDir);
            completed += 1;
            streak = { kind: null, count: 0 };
            output.appendLine(
              `${tag} ok   ${label} -> ${written.length ? written.join(", ") : "NO FILES PARSED"}`
            );
            break;
          } catch (error) {
            const problem = token.isCancellationRequested ? PROBLEMS.cancelled : classifyError(error);
            if (problem.kind === "cancelled") {
              stopped = problem; // nothing went wrong with the run, so nothing is recorded on it
              break;
            }
            if (problem.retry && attempt < MAX_ATTEMPTS) {
              output.appendLine(`${tag} ${problem.title} on ${label}: ${errorText(error)}`);
              output.appendLine(
                `    Waiting ${RETRY_WAIT_S}s, then retrying (attempt ${attempt + 1} of ${MAX_ATTEMPTS}).`
              );
              progress.report({ message: `${problem.title}: waiting ${RETRY_WAIT_S}s before retrying ${label}` });
              await sleepCancellable(RETRY_WAIT_S * 1000, token);
              if (token.isCancellationRequested) {
                stopped = PROBLEMS.cancelled;
                break;
              }
              continue;
            }

            failed += 1;
            failures[problem.kind] = (failures[problem.kind] || 0) + 1;
            recordError(item.runDir, problem, error, model);
            output.appendLine(`${tag} FAIL ${label}: ${problem.title}. ${errorText(error)}`);
            output.appendLine(`    ${problem.advice}`);

            streak = streak.kind === problem.kind ? { kind: problem.kind, count: streak.count + 1 } : { kind: problem.kind, count: 1 };
            // Retries used up means the next run would hit the same wall. The same error
            // several runs in a row usually means a limit Copilot is not naming, except for
            // oversize prompts, which are a property of each prompt rather than of the account.
            if (problem.stop || problem.retry) {
              stopped = problem;
            } else if (problem.kind !== "too_long" && streak.count >= STREAK_LIMIT) {
              stopped = {
                ...problem,
                advice: `${STREAK_LIMIT} runs in a row failed this way, so the batch stopped. ${problem.advice}`,
              };
            } else if (!warned.has(problem.kind)) {
              warned.add(problem.kind);
              vscode.window.showWarningMessage(`CI Memory: ${problem.title} on ${label}. ${problem.advice}`);
            }
            break;
          }
        }
        status.set({ completed, failed });
        if (stopped) break;
        if (settings.delaySeconds > 0) await sleep(settings.delaySeconds * 1000);
      }
    }
  );

  const causes = Object.entries(failures)
    .map(([kind, count]) => `${count} ${PROBLEMS[kind].title.toLowerCase()}`)
    .join(", ");
  const remaining = pending.length - completed - failed;
  output.appendLine(`\nDone. ${completed} succeeded, ${failed} failed${causes ? ` (${causes})` : ""}.`);
  if (failed) output.appendLine("Failed runs have agent_error.json and stay pending; the next batch retries them.");

  status.set({
    completed,
    failed,
    state: stopped ? (stopped.kind === "cancelled" ? "cancelled" : "stopped") : "done",
    message: stopped ? `${stopped.title}. ${stopped.advice}` : `${completed} succeeded, ${failed} failed.`,
  });

  if (stopped && stopped.kind === "cancelled") {
    output.appendLine("Cancelled.");
    vscode.window.showInformationMessage(
      `CI Memory: cancelled. ${completed} run(s) finished and are kept; ${remaining} not started.`
    );
  } else if (stopped) {
    output.appendLine(`\nSTOPPED: ${stopped.title}. ${stopped.advice}`);
    const actions = stopped.link ? [stopped.linkLabel, "Show log"] : ["Show log"];
    vscode.window
      .showErrorMessage(
        `CI Memory stopped: ${stopped.title}. ${completed} run(s) finished first, ${remaining} not started. ${stopped.advice}`,
        ...actions
      )
      .then((choice) => {
        if (choice === "Show log") output.show(true);
        else if (choice && stopped.link) vscode.env.openExternal(vscode.Uri.parse(stopped.link));
      });
  } else if (failed) {
    vscode.window
      .showWarningMessage(
        `CI Memory: ${completed} runs finished, ${failed} failed (${causes}). Failed runs stay pending; see the log.`,
        "Show log"
      )
      .then((choice) => {
        if (choice) output.show(true);
      });
  } else {
    vscode.window.showInformationMessage(`CI Memory: ${completed} runs finished. Score them in the dashboard.`);
  }
}

function activate(context) {
  context.subscriptions.push(
    // vscode://ci-memory-agents.ci-memory-agents-runner/run?agent=..&task=..&condition=..&request=..
    // Opened by scripts/run_copilot.py, which the dashboard's run buttons start in a
    // command window. Runs that task's pending runs in that arm.
    vscode.window.registerUriHandler({
      handleUri(uri) {
        const query = new URLSearchParams(uri.query);
        const agent = query.get("agent") || "";
        const task = query.get("task") || "";
        if (uri.path !== "/run" || !ALLOWED_AGENTS[agent] || !task) {
          vscode.window.showErrorMessage(
            `CI Memory: this link is not a run request for an allowed agent ` +
              `(${Object.keys(ALLOWED_AGENTS).join(", ")}) and a task: ${uri.toString()}`
          );
          return;
        }
        runBatch(task, {
          agent,
          condition: query.get("condition") || null,
          request: query.get("request") || null,
        });
      },
    }),

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
      output.appendLine(`Extension v${EXTENSION_VERSION}`);
      output.appendLine("Available Copilot models (use the family value in settings):\n");
      for (const model of models) {
        const cell = Object.keys(ALLOWED_AGENTS).find((name) => ALLOWED_AGENTS[name] === model.family);
        output.appendLine(
          `  family: ${model.family}${cell ? `   <- used by ${cell}` : ""}\n    id: ${model.id}\n    max input tokens: ${model.maxInputTokens}\n`
        );
      }
      const missing = Object.entries(ALLOWED_AGENTS).filter(([, family]) => !models.some((m) => m.family === family));
      for (const [cell, family] of missing) {
        output.appendLine(`Not available to this account: ${family} (needed by ${cell}).`);
      }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate, parseFiles, stripFences, classifyError, checkAgent, PROBLEMS };
