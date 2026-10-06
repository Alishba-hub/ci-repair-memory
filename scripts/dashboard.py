from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents import ci_jobs, design
from ci_memory_agents.dashboard_state import (
    collect,
    describe_design,
    export_csv,
    export_latex,
    list_agents,
    open_in_editor,
    prompt_for,
    reset_run,
    run_detail,
    score_task,
)
from ci_memory_agents.prompt_builder import CONDITIONS, condition_label
from ci_memory_agents.report import build as build_report

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_PATH = REPO_ROOT / "src" / "ci_memory_agents" / "ui.html"

# The report is written as page content; served on its own it needs a document shell.
REPORT_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>a.back{position:fixed;top:14px;right:16px;z-index:9;font:13px/1 -apple-system,"Segoe UI",Roboto,sans-serif;
text-decoration:none;padding:9px 14px;border-radius:8px;border:1px solid #8884;color:inherit;
background:color-mix(in srgb, Canvas 88%%, transparent)}</style>
</head><body><a class="back" href="/">&larr; Back to dashboard</a>
%s
</body></html>"""


def launch_copilot_run(runs_root: Path, body: dict) -> dict:
    """Open a command window that runs one task's arm through Copilot.

    The window runs scripts/run_copilot.py, which hands the request to the VS Code
    extension and prints its progress. `cmd /k` keeps the window open afterwards, so
    the outcome can still be read once the batch ends.
    """
    agent = body.get("agent") or ""
    task = body.get("task") or ""
    condition = body.get("condition") or ""
    if agent not in design.RUNNER_AGENTS:
        raise ValueError(
            f"Automatic runs are only available for {', '.join(design.RUNNER_AGENTS)}; "
            f"this page is showing {agent!r}."
        )
    if condition not in CONDITIONS:
        raise ValueError(f"unknown arm {condition!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", task) or ".." in task:
        raise ValueError(f"invalid task id {task!r}")
    if not (runs_root / agent / task / condition).is_dir():
        raise ValueError(
            f"No run folders for {task} / {condition}. Lay out the prompts first: "
            f"python scripts/run_experiment.py --mode prompts --agent {agent}"
        )
    command = [
        sys.executable, str(REPO_ROOT / "scripts" / "run_copilot.py"),
        "--agent", agent, "--task-id", task, "--condition", condition,
    ]
    if os.name == "nt":
        subprocess.Popen(["cmd", "/k", *command], cwd=REPO_ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(command, cwd=REPO_ROOT, start_new_session=True)
    return {
        "status": f"Started {condition_label(condition)} for {task} in a new command window.",
        "command": subprocess.list2cmdline(command),
    }


class Handler(BaseHTTPRequestHandler):
    tasks_root = REPO_ROOT / "tasks"
    runs_root = REPO_ROOT / "runs"
    agent = design.DASHBOARD_AGENT

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _send(self, payload, status: int = 200, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The browser navigated away or refreshed before the reply landed.
            # Harmless, and common with the page's polling; do not spam the console.
            pass

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        agent = query.get("agent") or self.agent
        # Only the two Copilot cells are shown. An older link such as ?agent=claude-code
        # falls back to the default rather than opening a cell the picker does not list.
        if agent not in design.RUNNER_AGENTS:
            agent = design.DASHBOARD_AGENT
        try:
            if url.path == "/":
                self._send(UI_PATH.read_bytes(), content_type="text/html; charset=utf-8")
            elif url.path == "/api/agents":
                self._send({"agents": list_agents(self.runs_root), "default": self.agent})
            elif url.path == "/api/tasks":
                self._send(
                    {
                        "agent": agent,
                        "agents": list_agents(self.runs_root),
                        # The page renders its arm columns, labels and colours from
                        # this rather than from a list written into the HTML, so an
                        # arm can never exist on disk and be invisible in the UI.
                        "design": describe_design(),
                        "runner_agents": list(design.RUNNER_AGENTS),
                        "tasks": collect(self.tasks_root, self.runs_root, agent),
                    }
                )
            elif url.path == "/api/prompt":
                text = prompt_for(
                    self.tasks_root,
                    self.runs_root,
                    agent,
                    query["task"],
                    query["condition"],
                    query["run"],
                )
                self._send({"prompt": text})
            elif url.path == "/api/run":
                self._send(
                    run_detail(
                        self.tasks_root,
                        self.runs_root,
                        agent,
                        query["task"],
                        query["condition"],
                        query["run"],
                    )
                )
            elif url.path == "/api/ci/reference":
                self._send(ci_jobs.status_reference(self.tasks_root, query["task"], query["kind"]))
            elif url.path == "/api/ci":
                self._send(
                    ci_jobs.status(
                        self.tasks_root, self.runs_root, agent, query["task"], query["condition"], query["run"]
                    )
                )
            elif url.path == "/api/score":
                self._send(score_task(self.tasks_root, self.runs_root, agent, query.get("task")))
            elif url.path == "/report":
                # Built live from the current runs, so the report is never stale.
                page = build_report(agent, self.tasks_root, self.runs_root)
                self._send(
                    (REPORT_SHELL % page).encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
            elif url.path == "/api/export":
                fmt = query.get("format", "csv")
                if fmt == "latex":
                    body = export_latex(self.tasks_root, self.runs_root, agent).encode("utf-8")
                    name, mime = f"{agent}_results.tex", "text/plain; charset=utf-8"
                else:
                    body = export_csv(self.tasks_root, self.runs_root, agent).encode("utf-8")
                    name, mime = f"{agent}_runs.csv", "text/csv; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send({"error": "not found"}, status=404)
        except Exception as error:
            self._send({"error": f"{type(error).__name__}: {error}"}, status=500)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        try:
            if url.path == "/api/agent/run":
                self._send(launch_copilot_run(self.runs_root, body))
                return
            if url.path == "/api/ci/reference":
                self._send(
                    ci_jobs.start_reference(
                        self.tasks_root, body["task"], body["kind"], REPO_ROOT / "runs" / "_repos"
                    )
                )
                return
            if url.path == "/api/ci/run-arm":
                self._send(
                    ci_jobs.start_arm(
                        self.tasks_root,
                        self.runs_root,
                        body.get("agent") or self.agent,
                        body["task"],
                        body["condition"],
                        REPO_ROOT / "runs" / "_repos",
                    )
                )
                return
            workspace = (
                self.runs_root
                / (body.get("agent") or self.agent)
                / body["task"]
                / body["condition"]
                / body["run"]
                / "workspace"
            )
            if url.path == "/api/open":
                self._send({"status": open_in_editor(workspace)})
            elif url.path == "/api/reset":
                repo_before = self.tasks_root / body["task"] / "repo_before"
                self._send({"status": reset_run(workspace, repo_before)})
            elif url.path == "/api/ci/run":
                self._send(
                    ci_jobs.start(
                        self.tasks_root,
                        self.runs_root,
                        body.get("agent") or self.agent,
                        body["task"],
                        body["condition"],
                        body["run"],
                        REPO_ROOT / "runs" / "_repos",
                    )
                )
            else:
                self._send({"error": "not found"}, status=404)
        except Exception as error:
            self._send({"error": f"{type(error).__name__}: {error}"}, status=500)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the experiment from a browser")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--agent", default=design.DASHBOARD_AGENT)
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    Handler.tasks_root = Path(args.tasks_root)
    Handler.runs_root = Path(args.runs_root)
    if args.agent not in design.RUNNER_AGENTS:
        print(
            f"  {args.agent!r} is not shown on the dashboard, which lists only "
            f"{', '.join(design.RUNNER_AGENTS)}. Opening {design.DASHBOARD_AGENT}. "
            f"Its runs are still on disk under runs/{args.agent}."
        )
        args.agent = design.DASHBOARD_AGENT
    Handler.agent = args.agent

    address = f"http://localhost:{args.port}"
    # On Windows the default SO_REUSEADDR lets a second dashboard bind a port that is
    # already in use. Both then answer, at random, and a page served by the new copy can
    # be answered by the old one, which looks like the results silently failing to draw.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("localhost", args.port), Handler)
    except OSError as error:
        print(f"\n  Port {args.port} is already in use ({error.strerror or error}).")
        print("  A dashboard is probably still running. Close it, or pick another port")
        print(f"  with --port {args.port + 1}.\n")
        return 1
    print("=" * 58)
    print("  CI Memory Agents - experiment runner")
    print("=" * 58)
    print(f"\n  Open this in your browser:  {address}\n")
    print(f"  Agent:  {args.agent}")
    print("  Stop:   press Ctrl+C\n")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
