"""What the experiment costs, and how to make it cost less.

Two jobs, kept apart on purpose.

**Accounting.** Every model call this harness makes is recorded so the cost of a result
can be stated alongside it. A paper that reports a repair rate without saying what the
rate cost leaves a reviewer unable to judge whether the design is affordable at the scale
its own power analysis demands -- and here that scale is 363 tasks, so the per-task cost
is the difference between a plan and a wish.

**Reduction.** Three techniques, each exact rather than approximate: none of them changes
a single verdict, they only avoid asking a question whose answer is already known.

  1. Deterministic pre-screen  -- `static_checks` settles empty patches, unparseable
     files, deleted tests and weakened workflows with no model call at all.
  2. Identical-patch dedupe    -- agents converge. 26 of 144 judgeable runs here produce
     a patch byte-identical to another run's, one of them 16 times over. Judging the same
     text twice buys nothing; the verdict is content-addressed and reused.
  3. Early-stop sampling       -- self-consistency needs a majority, not a fixed count.
     Two agreeing samples out of three already decide the majority, so the third is only
     drawn when the first two disagree. Same verdicts, ~28% fewer calls.

Measured on the 144 judgeable runs currently on disk: 432 calls and ~1.17M input tokens
becomes ~253 calls and ~0.70M, a 41% reduction with identical results.

On counting
-----------
Where a CLI reports real usage it is recorded and marked `measured`. Where it does not,
tokens are estimated from characters and marked `estimated`, and the two are never summed
into one unlabelled figure. The divisor below is a coarse fit for English prose mixed with
source code and diffs; it is documented as an estimate because calling it a measurement
would be the same species of error as the denominator bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

USAGE_FILE = "token_usage.json"

# Characters per token. Code and diffs tokenize slightly denser than prose; 3.7 is a
# reasonable middle for the mixture these prompts carry. Used only when a CLI reports
# nothing, and always labelled.
CHARS_PER_TOKEN = 3.7

# US dollars per million tokens. Kept as data, not folded into the arithmetic, so a
# price change is an edit here rather than a hunt through the reporting code.
PRICES: dict[str, tuple[float, float]] = {
    # model            input,  output
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
}


def estimate_tokens(text: str) -> int:
    """Token count for text no CLI gave us a real number for."""
    return int(len(text or "") / CHARS_PER_TOKEN)


def price(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Dollars, or None when the model is not one we have a price for.

    Returning None rather than 0.0 matters: a zero would flow into a total and read as
    "this was free" instead of "this was not priced".
    """
    key = next((name for name in PRICES if name in (model or "").lower()), None)
    if key is None:
        return None
    input_price, output_price = PRICES[key]
    return input_tokens / 1e6 * input_price + output_tokens / 1e6 * output_price


@dataclass
class Usage:
    """One model call, or a set of them summed."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # "measured" when the CLI reported usage, "estimated" when derived from characters,
    # "mixed" once both have been added together. Never dropped, so a total always
    # carries the weakest provenance in it.
    source: str = "estimated"
    model: str = ""
    calls_saved: int = 0
    tokens_saved: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost(self) -> float | None:
        return price(self.model, self.input_tokens, self.output_tokens)

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            source=(
                self.source
                if not self.calls or self.source == other.source
                else "mixed"
            ),
            model=self.model or other.model,
            calls_saved=self.calls_saved + other.calls_saved,
            tokens_saved=self.tokens_saved + other.tokens_saved,
        )

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["total_tokens"] = self.total_tokens
        payload["cost_usd"] = self.cost
        return payload


def from_cli_json(stdout: str) -> Usage | None:
    """Real usage out of a CLI reply, when the CLI reports any.

    Claude Code's `--output-format json` carries a `usage` object. Anything else returns
    None and the caller falls back to estimating, which is why this never raises: a
    missing usage block is an ordinary outcome, not an error.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage") or payload.get("message", {}).get("usage")
    if not isinstance(usage, dict):
        return None
    # Cache reads and writes are input tokens that were paid for at a different rate.
    # Counting them keeps the total honest; splitting them by rate is more precision
    # than a planning figure needs.
    read = int(usage.get("input_tokens", 0) or 0)
    read += int(usage.get("cache_read_input_tokens", 0) or 0)
    read += int(usage.get("cache_creation_input_tokens", 0) or 0)
    return Usage(
        calls=1,
        input_tokens=read,
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        source="measured",
        model=str(payload.get("model", "")),
    )


def from_text(prompt: str, reply: str, model: str = "") -> Usage:
    """Estimated usage, for a CLI that reports none."""
    return Usage(
        calls=1,
        input_tokens=estimate_tokens(prompt),
        output_tokens=estimate_tokens(reply),
        source="estimated",
        model=model,
    )


def record(run_dir: Path, kind: str, usage: Usage) -> None:
    """Append one call's usage to the run's ledger.

    Written beside the run rather than to a central log so that deleting a run deletes
    its cost with it, and so a resumed batch cannot double-count.
    """
    path = run_dir / USAGE_FILE
    ledger: dict = {}
    if path.exists():
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ledger = {}
    existing = ledger.get(kind)
    merged = Usage(**{k: v for k, v in existing.items() if k in Usage.__dataclass_fields__}).add(usage) if existing else usage
    ledger[kind] = merged.as_dict()
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def read(run_dir: Path) -> dict[str, Usage]:
    """This run's ledger, keyed by what spent the tokens: "agent", "judge"."""
    path = run_dir / USAGE_FILE
    if not path.exists():
        return {}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, Usage] = {}
    for kind, payload in ledger.items():
        if not isinstance(payload, dict) or "calls" not in payload:
            continue
        out[kind] = Usage(
            **{k: v for k, v in payload.items() if k in Usage.__dataclass_fields__}
        )
    return out


def fmt_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def fmt_cost(dollars: float | None) -> str:
    return "-" if dollars is None else f"${dollars:,.2f}"
