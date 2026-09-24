"""Scorecards and markdown research reports.

Wording rules enforced here: results are always labelled with the sample they
come from (in-sample, walk-forward out-of-sample, holdout test, paper, live).
No report states or implies that a strategy "is profitable"; a positive number
is described as a historical/simulated result under stated assumptions.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from alphalab.data.db import Database

DISCLAIMER = ("Simulated/historical results under explicit fill, fee and latency assumptions. They are not "
              "evidence of future profitability. Out-of-sample means walk-forward folds never used for "
              "parameter selection; 'holdout' is a final set evaluated at most once.")


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else (f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}")


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _num(x: Optional[float], nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    rows = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        rows.append("| " + " | ".join("" if (isinstance(v, float) and pd.isna(v)) else str(v) for v in r) + " |")
    return "\n".join(rows)


def scorecard(exp: Dict[str, Any]) -> str:
    """``exp`` is a row of the experiments table (json columns decoded)."""
    s = exp["summary"]
    oos, isd, test = s.get("oos") or {}, s.get("is") or {}, s.get("test")
    markets = sorted({m.split("-")[0] for b in s.get("blocks", []) for m in b.get("markets", [])})
    lines = [
        f"Strategy: {exp['strategy']}   (experiment {exp['experiment_id']}: {exp['name']})",
        f"Markets: {', '.join(markets) or 'n/a'}",
        f"Selected parameters (chosen on development data only): {json.dumps(exp['selected_params'], sort_keys=True)}",
        f"Grid points tried: {exp.get('n_trials')}",
        "",
        "Walk-forward OUT-OF-SAMPLE (never used for selection):",
        f"  Sample:               {oos.get('n_trades')} trades / {oos.get('n_fills')} fills over "
        f"{oos.get('n_markets')} markets, {oos.get('n_days')} days",
        f"  P&L at mid:           {_money(oos.get('mid_pnl'))}",
        f"  Gross P&L:            {_money(oos.get('gross_pnl'))}",
        f"  Fees:                 {_money(oos.get('fees'))}",
        f"  Estimated slippage:   {_money(oos.get('est_slippage'))}  (taker cost vs mid, already inside gross)",
        f"  Net P&L:              {_money(oos.get('net_pnl'))}",
        f"  Expectancy / trade:   {_money(oos.get('expectancy'))}",
        f"  Win rate:             {_pct(oos.get('win_rate'))}",
        f"  Profit factor:        {_num(oos.get('profit_factor'))}",
        f"  Max drawdown:         {_money(oos.get('max_drawdown'))}",
        f"  Sharpe (per trade):   {_num(oos.get('sharpe_per_trade'))}   Sharpe (daily, ann.): "
        f"{_num(oos.get('sharpe_daily_ann'))}",
        f"  Fill rate:            {_pct(oos.get('fill_rate'))}",
        f"  Adverse selection:    {_num(oos.get('adverse_selection_c'))} c/contract (30s markout, + = adverse)",
        f"  Avg holding:          {_num(oos.get('avg_holding_s'), 1)} s",
        "",
        f"In-sample expectancy:   {_money(isd.get('expectancy'))} on {isd.get('n_trades')} trades",
        f"Out-of-sample expect.:  {_money(oos.get('expectancy'))}",
        f"Holdout test:           " + ("not evaluated (candidate failed earlier gates)" if not test else
                                        f"{test.get('n_trades')} trades, net {_money(test.get('net_pnl'))}"),
    ]
    st = s.get("stress") or {}
    if st:
        lines.append("Cost/latency stress (OOS, net P&L): " + ", ".join(
            f"{k}={_money((v or {}).get('net_pnl'))}" for k, v in st.items()))
    rb = s.get("random_benchmark")
    if rb:
        lines.append(f"Random-direction benchmark: mean net {_money(rb['random_mean_net'])} over {rb['trials']} trials; "
                     f"p-value {rb['pvalue']:.3f}")
    if s.get("psr") is not None:
        lines.append(f"Probabilistic Sharpe (deflated for {exp.get('n_trials')} trials): {s['psr']:.3f}")
    flags = exp.get("flags") or []
    if flags:
        lines.append("Flags:")
        lines += [f"  [{'GATING' if f['gating'] else 'info'}] {f['code']}: {f['message']}" for f in flags]
    lines += ["", f"Status: {exp['classification']}  -  {s.get('reason', '')}", "", DISCLAIMER]
    return "\n".join(lines)


def load_experiments(db: Database) -> List[Dict[str, Any]]:
    df = db.query_df("SELECT * FROM experiments ORDER BY created_ns DESC")
    out = []
    for r in df.to_dict("records"):
        for k in ("spec", "flags", "summary", "selected_params"):
            r[k] = json.loads(r[k]) if r.get(k) else None
        out.append(r)
    return out


def data_inventory(db: Database) -> Dict[str, Any]:
    q = db.query_df
    inv = {
        "markets": int(q("SELECT COUNT(DISTINCT market) n FROM book_events")["n"].iloc[0]),
        "book_events": int(q("SELECT COUNT(*) n FROM book_events")["n"].iloc[0]),
        "trades": int(q("SELECT COUNT(*) n FROM trades")["n"].iloc[0]),
        "settlements": int(q("SELECT COUNT(*) n FROM settlements")["n"].iloc[0]),
        "settlements_inferred": int(q("SELECT COUNT(*) n FROM settlements WHERE inferred")["n"].iloc[0]),
        "sources": q("SELECT COALESCE(source,'?') AS src, COUNT(*) n FROM markets GROUP BY 1 ORDER BY 2 DESC")
        .to_dict("records"),
    }
    r = q("SELECT MIN(ts_ns) lo, MAX(ts_ns) hi FROM book_events")
    if r["lo"].iloc[0] is not None and not pd.isna(r["lo"].iloc[0]):
        inv["from"] = datetime.fromtimestamp(r["lo"].iloc[0] / 1e9, timezone.utc).isoformat()
        inv["to"] = datetime.fromtimestamp(r["hi"].iloc[0] / 1e9, timezone.utc).isoformat()
        inv["hours"] = round((r["hi"].iloc[0] - r["lo"].iloc[0]) / 3.6e12, 2)
    return inv


def research_report(db: Database, out_dir: str | Path = "reports", title: str = "Research report",
                    extra_sections: Optional[List[str]] = None) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inv = data_inventory(db)
    exps = load_experiments(db)
    lines = [f"# {title}", "", f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "",
             f"> {DISCLAIMER}", "", "## Data", "", "```", json.dumps(inv, indent=2, default=str), "```", ""]
    lines += ["## Experiment results", "",
              "| experiment | strategy | status | OOS trades | OOS net | OOS expectancy | holdout net | gating flags |",
              "|---|---|---|---|---|---|---|---|"]
    for e in exps:
        o = (e["summary"] or {}).get("oos") or {}
        t = (e["summary"] or {}).get("test") or {}
        g = ",".join(f["code"] for f in (e["flags"] or []) if f["gating"])
        lines.append(f"| {e['name']} | {e['strategy']} | {e['classification']} | {o.get('n_trades')} | "
                     f"{_money(o.get('net_pnl'))} | {_money(o.get('expectancy'))} | "
                     f"{_money(t.get('net_pnl')) if t else 'not run'} | {g} |")
    lines.append("")
    pr = db.query_df("""SELECT study_id, feature, horizon_s, split, n, ic, ic_tstat, hit_rate, cost_hurdle,
                               net_edge_top, net_edge_bottom FROM predictive_results
                        WHERE study_id = (SELECT study_id FROM predictive_results ORDER BY created_ns DESC LIMIT 1)
                        ORDER BY feature, horizon_s, split""")
    pr = pr[pr["ic"].notna()] if not pr.empty else pr
    if not pr.empty:
        lines += ["## Latest predictive-feature study", "",
                  "IC = Spearman rank correlation between the feature and the forward mid move, computed on "
                  "non-overlapping samples. Cost hurdle = mean spread + 2 x taker fee (cents).", "",
                  md_table(pr.round(4)), ""]
    for sec in extra_sections or []:
        lines += [sec, ""]
    lines += ["## Scorecards", ""]
    for e in exps:
        lines += ["```", scorecard(e), "```", ""]
    path = out_dir / f"research_report_{int(time.time())}.md"
    path.write_text("\n".join(lines))
    return path
