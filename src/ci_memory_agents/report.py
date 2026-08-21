from __future__ import annotations

import html
import statistics
from collections import defaultdict
from pathlib import Path

from .dashboard_state import score_task
from .loader import list_tasks
from .prompt_builder import CONDITIONS

LABEL = {"no_memory": "Without memory", "with_memory": "With memory"}
SLOT = {"no_memory": "1", "with_memory": "2"}


def collect(tasks_root: Path, runs_root: Path, agent: str) -> dict:
    data = score_task(tasks_root, runs_root, agent, None)
    meta = {t.task_id: t for t in list_tasks(tasks_root)}

    per_task: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in data["results"]:
        per_task[row["task_id"]][row["condition"]].append(row)
    for task in per_task:
        for condition in per_task[task]:
            per_task[task][condition].sort(key=lambda r: r["run"])
    return {"summary": data["summary"], "results": data["results"], "per_task": per_task, "meta": meta}


def rate(rows: list[dict]) -> float | None:
    return sum(1 for r in rows if r["normalized_match"]) / len(rows) if rows else None


def bar_chart(rows: list[tuple[str, dict[str, tuple[int, int]]]], title: str, note: str) -> str:
    """Horizontal grouped bars: solved fraction per condition, one group per row.

    Horizontal because the category labels are long identifiers; a vertical axis
    would force rotated text, which is slower to read.
    """
    if not rows:
        return ""
    row_h, bar_h, gap, label_w, right = 46, 13, 4, 210, 66
    width, plot_w = 860, 860 - label_w - right
    height = len(rows) * row_h + 34

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}" '
        f'style="width:100%;height:auto;overflow:visible">'
    ]
    for x in (0, 0.25, 0.5, 0.75, 1.0):
        gx = label_w + x * plot_w
        parts.append(
            f'<line x1="{gx:.1f}" y1="18" x2="{gx:.1f}" y2="{height - 22}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
            f'<text x="{gx:.1f}" y="{height - 8}" fill="var(--text-muted)" font-size="10.5" '
            f'text-anchor="middle">{int(x * 100)}%</text>'
        )

    for index, (name, values) in enumerate(rows):
        top = 22 + index * row_h
        parts.append(
            f'<text x="{label_w - 10}" y="{top + row_h / 2 - 3}" fill="var(--text-primary)" '
            f'font-size="12" text-anchor="end">{html.escape(name[:30])}</text>'
        )
        for offset, condition in enumerate(CONDITIONS):
            solved, total = values.get(condition, (0, 0))
            y = top + offset * (bar_h + gap)
            if total == 0:
                parts.append(
                    f'<text x="{label_w + 6}" y="{y + bar_h - 2}" fill="var(--text-muted)" '
                    f'font-size="10.5">not run</text>'
                )
                continue
            w = max(2.0, (solved / total) * plot_w)
            parts.append(
                f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
                f'fill="var(--series-{SLOT[condition]})"><title>{html.escape(name)} — '
                f'{LABEL[condition]}: {solved} of {total} runs fixed</title></rect>'
                f'<text x="{label_w + w + 7:.1f}" y="{y + bar_h - 2}" fill="var(--text-secondary)" '
                f'font-size="11">{solved}/{total}</text>'
            )
    parts.append("</svg>")

    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px">'
        f'<i style="width:11px;height:11px;border-radius:3px;background:var(--series-{SLOT[c]})"></i>'
        f'<span>{LABEL[c]}</span></span>'
        for c in CONDITIONS
    )
    return (
        f'<figure class="fig"><figcaption class="figtitle">{html.escape(title)}</figcaption>'
        f'<div class="legend">{legend}</div>{"".join(parts)}'
        f'<p class="note">{html.escape(note)}</p></figure>'
    )


def run_matrix(per_task: dict, meta: dict) -> str:
    """One cell per run: filled = matched the real fix, hollow = did not."""
    blocks = []
    for task in sorted(per_task):
        conditions = per_task[task]
        name = meta[task].repo_name if task in meta else task
        errors = ", ".join(meta[task].error_type) if task in meta else ""
        rows = []
        for condition in CONDITIONS:
            runs = conditions.get(condition, [])
            cells = "".join(
                f'<i class="cell {"on" if r["normalized_match"] else "off"} s{SLOT[condition]}" '
                f'title="{r["run"]}: {"matched the real fix" if r["normalized_match"] else "did not match"}'
                f' — files found {r["file_recall"]:.0%}"></i>'
                for r in runs
            )
            solved = sum(1 for r in runs if r["normalized_match"])
            summary = f"{solved}/{len(runs)}" if runs else "not run"
            pct = f"{solved / len(runs):.0%}" if runs else "—"
            rows.append(
                f'<tr><td class="cond">{LABEL[condition]}</td>'
                f'<td class="cells">{cells or "<span class=\'muted\'>no runs yet</span>"}</td>'
                f'<td class="num">{summary}</td><td class="num">{pct}</td></tr>'
            )
        blocks.append(
            f'<div class="taskcard"><div class="taskhead"><strong>{html.escape(name)}</strong>'
            f'<div class="id">{html.escape(task)}</div>'
            f'<div class="muted">{html.escape(errors)}</div></div>'
            f'<table class="matrix"><tbody>{"".join(rows)}</tbody></table></div>'
        )
    return "".join(blocks)


def detail_table(results: list[dict]) -> str:
    head = (
        "<tr><th>Task</th><th>Condition</th><th>Run</th><th>Fixed</th>"
        "<th class='num'>Files found</th><th class='num'>Precision</th>"
        "<th class='num'>IoU</th><th class='num'>Size vs real fix</th></tr>"
    )
    rows = []
    for r in sorted(results, key=lambda r: (r["task_id"], r["condition"], r["run"])):
        fixed = "yes" if r["normalized_match"] else "no"
        cls = "yes" if r["normalized_match"] else ""
        rows.append(
            f'<tr><td>{html.escape(r["task_id"])}</td><td>{LABEL[r["condition"]]}</td>'
            f'<td>{r["run"]}</td><td class="{cls}">{fixed}</td>'
            f'<td class="num">{r["file_recall"]:.0%}</td>'
            f'<td class="num">{r["file_precision"]:.0%}</td>'
            f'<td class="num">{r["file_iou"]:.2f}</td>'
            f'<td class="num">{r["line_deviation_ratio"]:.2f}</td></tr>'
        )
    return f'<table class="data"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table>'


def build(agent: str, tasks_root: Path, runs_root: Path) -> str:
    data = collect(tasks_root, runs_root, agent)
    summary, per_task, meta = data["summary"], data["per_task"], data["meta"]
    analysis = summary["analysis"]

    task_rows = []
    for task in sorted(per_task):
        name = meta[task].repo_name if task in meta else task
        values = {
            c: (sum(1 for r in per_task[task].get(c, []) if r["normalized_match"]), len(per_task[task].get(c, [])))
            for c in CONDITIONS
        }
        task_rows.append((name, values))

    error_rows = [
        (
            e["error_type"],
            {c: (e[c]["solved"], e[c]["runs"]) for c in CONDITIONS},
        )
        for e in summary["by_error_type"]
    ]

    recall = {
        c: statistics.mean([r["file_recall"] for r in data["results"] if r["condition"] == c] or [0])
        for c in CONDITIONS
    }
    total_runs = len(data["results"])
    solved_runs = sum(1 for r in data["results"] if r["normalized_match"])

    pc = lambda v: f"{v * 100:.1f}%"
    ci = lambda c: f"[{pc(c['low'])}, {pc(c['high'])}]"

    verdict = (
        "No difference detected"
        if analysis["discordant"] == 0
        else ("Memory solved more tasks" if analysis["only_with_memory"] > analysis["only_no_memory"]
              else "Memory solved fewer tasks")
    )

    return f"""<title>CI Memory Experiment Results</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --surface-2: #f4f3f0; --border: #dedcd6;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7c7a74;
    --grid: #e7e5df; --series-1: #2a78d6; --series-2: #eb6834; --good: #1baf7a;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1: #1a1a19; --surface-2: #232322; --border: #3a3a37;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #97968c;
      --grid: #33332f; --series-1: #3987e5; --series-2: #d95926; --good: #199e70;
    }}
  }}
  :root[data-theme="dark"] .viz-root {{
    color-scheme: dark;
    --surface-1: #1a1a19; --surface-2: #232322; --border: #3a3a37;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #97968c;
    --grid: #33332f; --series-1: #3987e5; --series-2: #d95926; --good: #199e70;
  }}
  body {{ margin:0; background: var(--surface-1); color: var(--text-primary);
         font-family: -apple-system, "Segoe UI", Roboto, sans-serif; line-height:1.55;
         --mono: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace; }}
  .viz-root {{ max-width: 940px; margin: 0 auto; padding: 40px 22px 90px; }}
  h1 {{ font-size: 28px; margin: 0 0 6px; letter-spacing:-.022em; text-wrap: balance;
        max-width: 22ch; line-height:1.2; }}
  h2 {{ font-size: 12px; margin: 46px 0 14px; letter-spacing:.09em; text-transform:uppercase;
        color: var(--text-secondary); font-weight:650;
        padding-bottom:7px; border-bottom:1px solid var(--border); }}
  h3 {{ font-size: 14px; margin: 26px 0 8px; color: var(--text-secondary); }}
  p {{ margin: 0 0 12px; max-width: 68ch; }}
  .sub {{ color: var(--text-secondary); margin-bottom: 26px; }}
  .muted {{ color: var(--text-muted); }}
  .note {{ color: var(--text-muted); font-size: 12.5px; margin: 8px 0 0; }}
  .hero {{ display:flex; gap:34px; flex-wrap:wrap; padding:20px 22px; background:var(--surface-2);
           border:1px solid var(--border); border-radius:12px; margin-bottom:8px; }}
  .hero div span {{ display:block; font-size:12px; color: var(--text-secondary); }}
  .hero b {{ font-size:25px; font-weight:600; font-variant-numeric: tabular-nums; }}
  .hero div span {{ text-transform:uppercase; letter-spacing:.055em; font-size:10.5px; }}
  .verdict {{ font-size:22px; font-weight:650; margin: 30px 0 4px; letter-spacing:-.015em; }}
  table {{ width:100%; border-collapse: collapse; font-size:13.5px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); }}
  th {{ color: var(--text-secondary); font-weight:600; font-size:11.5px;
        text-transform:uppercase; letter-spacing:.045em; }}
  td.num, th.num {{ text-align:right; font-variant-numeric: tabular-nums;
                    font-family: var(--mono); font-size:12.5px; }}
  .id {{ font-family: var(--mono); font-size:12px; color: var(--text-muted); }}
  .hero b {{ font-family: var(--mono); }}
  table.data td:first-child, table.data td:nth-child(3) {{ font-family: var(--mono); font-size:12px; }}
  td.yes {{ color: var(--good); font-weight:600; }}
  .scroll {{ overflow-x:auto; }}
  .fig {{ margin: 0 0 10px; }}
  .figtitle {{ font-size:13.5px; font-weight:600; margin-bottom:2px; }}
  .legend {{ display:flex; flex-wrap:wrap; font-size:12.5px; color:var(--text-secondary);
             margin:6px 0 10px; }}
  .taskcard {{ border:1px solid var(--border); border-radius:10px; padding:14px 16px;
               margin-bottom:10px; background:var(--surface-2); }}
  .taskhead {{ font-size:13.5px; margin-bottom:8px; }}
  table.matrix td {{ border:0; padding:3px 8px 3px 0; font-size:12.5px; vertical-align:middle; }}
  td.cond {{ color: var(--text-secondary); width:130px; }}
  .cell {{ display:inline-block; width:15px; height:15px; border-radius:3px; margin-right:3px;
           vertical-align:middle; }}
  .cell.off {{ background: transparent; border:1.5px solid var(--border); }}
  .cell.on.s1 {{ background: var(--series-1); }}
  .cell.on.s2 {{ background: var(--series-2); }}
  .callout {{ border-left:3px solid var(--series-1); background:var(--surface-2);
              padding:12px 16px; border-radius:0 8px 8px 0; margin:14px 0; }}
</style>

<div class="viz-root">
<h1>Does repository memory help an AI agent repair CI failures?</h1>
<p class="sub">Agent under test: <strong>{html.escape(agent)}</strong> ·
   {total_runs} scored runs across {len(per_task)} tasks from CI-Repair-Bench</p>

<div class="hero">
  <div><span>Tasks compared in both conditions</span><b>{analysis['n_tasks']}</b></div>
  <div><span>Solved without memory</span><b>{analysis['both_solved'] + analysis['only_no_memory']}/{analysis['n_tasks']}</b></div>
  <div><span>Solved with memory</span><b>{analysis['both_solved'] + analysis['only_with_memory']}/{analysis['n_tasks']}</b></div>
  <div><span>Tasks where they differ</span><b>{analysis['discordant']}</b></div>
  <div><span>Exact McNemar</span><b>p = {analysis['p_value']}</b></div>
</div>

<div class="verdict">{verdict}</div>
<p>Of {analysis['n_tasks']} tasks run under both conditions,
   <strong>{analysis['neither_solved']}</strong> were solved by neither,
   <strong>{analysis['both_solved']}</strong> by both,
   <strong>{analysis['only_no_memory']}</strong> only without memory, and
   <strong>{analysis['only_with_memory']}</strong> only with memory.</p>

<div class="callout">{html.escape(analysis['interpretation'])}</div>

<h2>1. Every run, task by task</h2>
<p>One square per run. A filled square means that run produced a fix matching the
   real developer fix; an outline means it did not. Hover any square for its detail.</p>
<div class="legend">
  <span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px">
    <i class="cell on s1"></i><span>Without memory — fixed</span></span>
  <span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px">
    <i class="cell on s2"></i><span>With memory — fixed</span></span>
  <span style="display:inline-flex;align-items:center;gap:6px">
    <i class="cell off"></i><span>not fixed</span></span>
</div>
{run_matrix(per_task, meta)}

<h2>2. Success rate per task</h2>
{bar_chart(task_rows, "Runs that matched the real fix, per task", "Bars show solved runs as a share of runs attempted for that task and condition.")}

<h2>3. Success rate by failure type</h2>
<p>Project precedent should plausibly help on dependency and environment failures and
   not on syntax errors, so a single pooled number can hide opposite effects.</p>
{bar_chart(error_rows, "Runs that matched the real fix, by CI failure category", "A task with several categories is counted under each of them, so runs sum to more than the total.")}

<h2>4. Statistical analysis</h2>
<table>
 <tbody>
  <tr><th>Paired tasks</th><td class="num">{analysis['n_tasks']}</td></tr>
  <tr><th>Solved without memory</th><td class="num">{pc(analysis['rate_no_memory'])} &nbsp;<span class="muted">95% CI {ci(analysis['ci_no_memory'])}</span></td></tr>
  <tr><th>Solved with memory</th><td class="num">{pc(analysis['rate_with_memory'])} &nbsp;<span class="muted">95% CI {ci(analysis['ci_with_memory'])}</span></td></tr>
  <tr><th>Discordant pairs</th><td class="num">{analysis['discordant']} &nbsp;<span class="muted">{analysis['only_no_memory']} without-only + {analysis['only_with_memory']} with-only</span></td></tr>
  <tr><th>Exact McNemar (two-sided)</th><td class="num">p = {analysis['p_value']} ({'significant' if analysis['significant'] else 'not significant'})</td></tr>
  <tr><th>Mean files correctly located</th><td class="num">{recall['no_memory']:.0%} without · {recall['with_memory']:.0%} with</td></tr>
  <tr><th>Runs matching the real fix</th><td class="num">{solved_runs}/{total_runs} ({solved_runs / total_runs:.0%})</td></tr>
 </tbody>
</table>
<p class="note">Wilson intervals are used rather than the normal approximation, which
   collapses to zero width at rates near zero. McNemar is computed exactly rather than
   by chi-square, which is invalid at this sample size. Fewer than
   {summary['min_discordant_for_significance']} discordant pairs can never reach p&lt;0.05,
   so below that a null result reflects sample size, not the absence of an effect.</p>

<h2>5. Every run in full</h2>
<p>"Files found" is the share of files the real fix touched that the agent also edited.
   "Size vs real fix" is how much larger the agent's change was: 0 means the same number
   of changed lines, 2 means three times as many.</p>
<div class="scroll">{detail_table(data['results'])}</div>

<h2>How to read this</h2>
<p><strong>Fixed</strong> means the agent's code matched the real developer fix after
   ignoring whitespace and comments. It is a strict bar: a fix that works but is written
   differently counts as not fixed. That is the main limitation of these numbers, and the
   reason so many runs score zero in both conditions.</p>
<p>A task counts as solved if <em>any</em> of its runs matched, which is the definition the
   McNemar test uses, so the tables and the test always agree.</p>
{f'<p class="note">{len(summary["timed_out"])} run(s) were cut off by a timeout and are excluded, since a killed run is a partial answer rather than a wrong one.</p>' if summary.get("timed_out") else ""}
</div>
"""
