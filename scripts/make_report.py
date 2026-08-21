from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ci_memory_agents.report import build

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a full results report")
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--tasks-root", default=str(REPO_ROOT / "tasks"))
    parser.add_argument("--runs-root", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out = Path(args.out) if args.out else REPO_ROOT / f"report_{args.agent}.html"
    out.write_text(
        build(args.agent, Path(args.tasks_root), Path(args.runs_root)), encoding="utf-8"
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
