"""Optional LLM research assistant (read-only).

The assistant answers questions about stored research ("What caused this
strategy to lose money?", "Is performance concentrated in one market?",
"Does the edge survive 100ms extra latency / 2x fees?", "Is this overfit?").

Guarantees:
* It can only call the read/analysis tools defined below. Every number comes
  from ``research.analysis`` / the database; the system prompt forbids
  inventing statistics and requires citing the tool output used.
* It has no tool that places, cancels or modifies orders, and this module
  does not import any execution code. It cannot change strategy status.
* Without ``ANTHROPIC_API_KEY`` (or with ALPHALAB_LLM unset) ``answer_offline``
  runs the same analyses deterministically and prints their raw output.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from alphalab.data.db import Database
from alphalab.research import analysis
from alphalab.research.reports import data_inventory

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the research assistant for Kalshi Alpha Lab, a system that tests whether trading
strategies on Kalshi prediction markets have a positive net expectancy after fees, slippage, fill risk and
adverse selection.

Rules:
- Every number you state must come from a tool result in this conversation. Never estimate, extrapolate or
  invent statistics. If a tool cannot answer, say what data is missing.
- Always say which sample a number comes from: in-sample, walk-forward out-of-sample, holdout test, paper, or
  live. Never describe a strategy as profitable because a backtest is positive; describe results as
  historical/simulated under the stated assumptions.
- Consider sample size, concentration in one market or day, sensitivity to fees/slippage/latency, and the
  validation flags before drawing conclusions.
- You cannot place or change orders or strategy status; do not offer to.
- Be concise. Cite the tool(s) used for each claim, e.g. "(concentration)"."""

TOOLS: List[Dict[str, Any]] = [
    {"name": "list_experiments", "description": "List stored experiments with id, strategy, status and "
     "out-of-sample net P&L / trade count.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "data_inventory", "description": "Summary of recorded market data: markets, events, date range, "
     "sources, inferred settlements.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "overfit_assessment", "description": "Validation evidence for one experiment: in-sample vs "
     "out-of-sample vs holdout metrics, walk-forward folds, parameter sensitivity, deflated Sharpe, random "
     "benchmark, and flags.", "input_schema": {"type": "object", "properties": {
         "experiment_id": {"type": "string"}}, "required": ["experiment_id"]}},
    {"name": "concentration", "description": "Out-of-sample P&L by market and by day, top-market share, and "
     "net P&L without the best market.", "input_schema": {"type": "object", "properties": {
         "experiment_id": {"type": "string"}}, "required": ["experiment_id"]}},
    {"name": "loss_attribution", "description": "Decompose out-of-sample P&L into mid-price P&L, execution "
     "cost vs mid, fees, and markouts (adverse selection); breakdown by exit reason and direction.",
     "input_schema": {"type": "object", "properties": {"experiment_id": {"type": "string"}},
                      "required": ["experiment_id"]}},
    {"name": "rerun_oos", "description": "Deterministically re-run the experiment's selected parameters on "
     "its out-of-sample blocks under modified assumptions (fee multiplier, extra taker slippage ticks, extra "
     "latency in ms, maker fee rate, or excluding trades entered in the first N seconds of a market).",
     "input_schema": {"type": "object", "properties": {
         "experiment_id": {"type": "string"},
         "fee_stress": {"type": "number", "description": "fee multiplier, e.g. 2.0"},
         "extra_slippage_ticks": {"type": "integer"},
         "extra_latency_ms": {"type": "integer"},
         "exclude_first_s": {"type": "number"},
         "maker_fee_rate": {"type": "number"}}, "required": ["experiment_id"]}},
]


def _dispatch(db: Database) -> Dict[str, Callable[..., Any]]:
    return {
        "list_experiments": lambda: analysis.list_experiments(db),
        "data_inventory": lambda: data_inventory(db),
        "overfit_assessment": lambda experiment_id: analysis.overfit_assessment(db, experiment_id),
        "concentration": lambda experiment_id: analysis.concentration(db, experiment_id),
        "loss_attribution": lambda experiment_id: analysis.loss_attribution(db, experiment_id),
        "rerun_oos": lambda experiment_id, **kw: analysis.rerun_oos(db, experiment_id, **{
            k: v for k, v in kw.items() if k in ("fee_stress", "extra_slippage_ticks", "extra_latency_ms",
                                                 "exclude_first_s", "maker_fee_rate")}),
    }


def run_tool(db: Database, name: str, args: Dict[str, Any]) -> str:
    fns = _dispatch(db)
    if name not in fns:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        return json.dumps(fns[name](**(args or {})), default=str)[:60_000]
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def answer(db: Database, question: str, model: str = "claude-opus-5", max_turns: int = 12,
           max_tokens: int = 16000) -> str:
    """Answer with Claude using only the read-only analysis tools."""
    import anthropic

    client = anthropic.Anthropic()
    messages: List[Dict[str, Any]] = [{"role": "user", "content": question}]
    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=model, max_tokens=max_tokens, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
            thinking={"type": "adaptive"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "The model declined to answer this request."
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            return "\n".join(b.text for b in response.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for tu in tool_uses:
            out = run_tool(db, tu.name, dict(tu.input or {}))
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out,
                            **({"is_error": True} if out.startswith('{"error"') else {})})
        messages.append({"role": "user", "content": results})
    return "Stopped: too many tool-use turns without a final answer."


def answer_offline(db: Database, question: str, experiment_id: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic fallback: pick analyses by keyword and return their raw output."""
    q = question.lower()
    exp = experiment_id
    if exp is None:
        exps = analysis.list_experiments(db)
        exp = exps[0]["experiment_id"] if exps else None
    out: Dict[str, Any] = {"question": question, "experiment_id": exp,
                           "note": "offline mode: raw analysis output, no LLM interpretation"}
    if exp is None:
        out["data_inventory"] = data_inventory(db)
        return out
    if any(w in q for w in ("concentrat", "one market", "one day", "single market")):
        out["concentration"] = analysis.concentration(db, exp)
    if any(w in q for w in ("lose", "loss", "lost", "why")):
        out["loss_attribution"] = analysis.loss_attribution(db, exp)
    if any(w in q for w in ("overfit", "robust", "valid")):
        out["overfit_assessment"] = analysis.overfit_assessment(db, exp)
    if "latency" in q:
        out["latency_100ms"] = analysis.rerun_oos(db, exp, extra_latency_ms=100)
    if "fee" in q:
        out["fees_x2"] = analysis.rerun_oos(db, exp, fee_stress=2.0)
    if "slippage" in q:
        out["slippage_1tick"] = analysis.rerun_oos(db, exp, extra_slippage_ticks=1)
        out["slippage_2tick"] = analysis.rerun_oos(db, exp, extra_slippage_ticks=2)
    if "first minute" in q or "first 60" in q:
        out["exclude_first_minute"] = analysis.rerun_oos(db, exp, exclude_first_s=60)
    if len(out) == 3:
        out["overfit_assessment"] = analysis.overfit_assessment(db, exp)
    return out
