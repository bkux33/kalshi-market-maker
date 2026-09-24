"""Deterministic analyses over stored experiments and runs.

These functions compute every number the research assistant is allowed to
quote. The LLM layer only calls them and interprets their output; it never
computes statistics itself.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Dict, List, Optional

import pandas as pd

from alphalab.core.config import FeeConfig, FillConfig, research_risk_config
from alphalab.data.db import Database
from alphalab.sim.backtest import BacktestConfig, run_backtest
from alphalab.sim.replay import ReplaySource, ReplaySpec
from alphalab.strategies import get_strategy

NS = 1_000_000_000


def get_experiment(db: Database, experiment_id: str) -> Dict[str, Any]:
    df = db.query_df("SELECT * FROM experiments WHERE experiment_id = ?", [experiment_id])
    if df.empty:
        raise KeyError(f"experiment {experiment_id} not found")
    r = df.iloc[0].to_dict()
    for k in ("spec", "flags", "summary", "selected_params"):
        r[k] = json.loads(r[k]) if r.get(k) else None
    return r


def list_experiments(db: Database) -> List[Dict[str, Any]]:
    df = db.query_df("""SELECT experiment_id, name, strategy, classification, created_ns,
                               json_extract(summary, '$.oos.net_pnl') AS oos_net,
                               json_extract(summary, '$.oos.n_trades') AS oos_trades FROM experiments
                        ORDER BY created_ns DESC""")
    return df.to_dict("records")


def oos_trips(db: Database, experiment_id: str) -> pd.DataFrame:
    return db.query_df("""SELECT rt.*, r.phase FROM round_trips rt JOIN runs r ON r.run_id = rt.run_id
                          WHERE r.experiment_id = ? AND r.phase LIKE 'wf_oos%' ORDER BY rt.entry_ns""",
                       [experiment_id])


def concentration(db: Database, experiment_id: str) -> Dict[str, Any]:
    t = oos_trips(db, experiment_id)
    if t.empty:
        return {"n_trades": 0, "note": "no out-of-sample trades stored"}
    t["day"] = pd.to_datetime(t["exit_ns"].where(t["exit_ns"] > 0, t["entry_ns"]), unit="ns", utc=True).dt.date.astype(str)
    by_m = t.groupby("market")["net"].agg(["sum", "count"]).sort_values("sum", ascending=False)
    by_d = t.groupby("day")["net"].agg(["sum", "count"]).sort_values("sum", ascending=False)
    total = float(t["net"].sum())
    pos_m = by_m["sum"][by_m["sum"] > 0].sum()
    return {"n_trades": int(len(t)), "total_net": total, "n_markets": int(len(by_m)), "n_days": int(len(by_d)),
            "top_market": by_m.index[0], "top_market_net": float(by_m["sum"].iloc[0]),
            "top_market_share_of_positive": float(by_m["sum"].iloc[0] / pos_m) if pos_m > 0 else None,
            "net_without_top_market": float(total - by_m["sum"].iloc[0]),
            "by_market": {k: {"net": float(v["sum"]), "trades": int(v["count"])} for k, v in by_m.head(20).iterrows()},
            "by_day": {k: {"net": float(v["sum"]), "trades": int(v["count"])} for k, v in by_d.iterrows()}}


def loss_attribution(db: Database, experiment_id: str) -> Dict[str, Any]:
    e = get_experiment(db, experiment_id)
    o = (e["summary"] or {}).get("oos") or {}
    t = oos_trips(db, experiment_id)
    out = {"mid_pnl": o.get("mid_pnl"), "exec_cost_vs_mid": o.get("exec_cost_vs_mid"), "fees": o.get("fees"),
           "net_pnl": o.get("net_pnl"), "adverse_selection_c": o.get("adverse_selection_c"),
           "markout_5s_all_c": o.get("markout_5s_all_c"), "markout_30s_all_c": o.get("markout_30s_all_c"),
           "explanation_inputs": "net = mid_pnl - exec_cost_vs_mid - fees"}
    if not t.empty:
        out["by_exit_reason"] = t.groupby("exit_reason")["net"].agg(["sum", "count"]).to_dict("index")
        out["by_direction"] = t.groupby("direction")["net"].agg(["sum", "count"]).to_dict("index")
    return out


def overfit_assessment(db: Database, experiment_id: str) -> Dict[str, Any]:
    e = get_experiment(db, experiment_id)
    s = e["summary"] or {}
    return {"classification": e["classification"], "reason": s.get("reason"), "grid_points": e.get("n_trials"),
            "in_sample": s.get("is"), "out_of_sample": s.get("oos"), "holdout": s.get("test"),
            "walk_forward_folds": s.get("wf_folds"), "sensitivity": s.get("sensitivity"), "psr": s.get("psr"),
            "random_benchmark": s.get("random_benchmark"), "flags": e["flags"]}


def rerun_oos(db: Database, experiment_id: str, fee_stress: float = 1.0, extra_slippage_ticks: int = 0,
              extra_latency_ms: int = 0, exclude_first_s: Optional[float] = None,
              maker_fee_rate: Optional[float] = None) -> Dict[str, Any]:
    """Re-run the selected parameters on the walk-forward OOS blocks under
    modified assumptions. ``exclude_first_s`` drops round trips entered within
    the first N seconds after market open (post-hoc filter - an approximation,
    reported as such)."""
    e = get_experiment(db, experiment_id)
    spec = e["spec"]
    fill = FillConfig(**spec["fill"])
    fill = replace(fill, taker_slippage_ticks=fill.taker_slippage_ticks + extra_slippage_ticks,
                   order_latency_ms=fill.order_latency_ms + extra_latency_ms,
                   cancel_latency_ms=fill.cancel_latency_ms + extra_latency_ms)
    fees = FeeConfig(**spec["fees"])
    if maker_fee_rate is not None:
        fees = replace(fees, maker_rate=maker_fee_rate)
    cls = get_strategy(e["strategy"])
    params = e["selected_params"]
    blocks = (e["summary"] or {}).get("blocks") or []
    results = []
    for b in blocks[1:]:
        rs = ReplaySpec(markets=b["markets"], start_ns=b.get("start_ns"), end_ns=b.get("end_ns"))
        cfg = BacktestConfig(replay=rs, fill=fill, fees=fees, fee_stress=fee_stress, risk=research_risk_config())
        results.append(run_backtest(db, lambda: cls(**params), cfg, persist=False))
    from alphalab.research.experiments import aggregate
    m = aggregate(results)
    out = {"assumptions": {"fee_stress": fee_stress, "extra_slippage_ticks": extra_slippage_ticks,
                           "extra_latency_ms": extra_latency_ms, "maker_fee_rate": fees.maker_rate},
           "net_pnl": m.get("net_pnl"), "n_trades": m.get("n_trades"), "expectancy": m.get("expectancy"),
           "fees": m.get("fees"), "gross_pnl": m.get("gross_pnl"),
           "baseline_oos_net": ((e["summary"] or {}).get("oos") or {}).get("net_pnl")}
    if exclude_first_s is not None:
        trips = pd.concat([r.trips for r in results if not r.trips.empty]) if results else pd.DataFrame()
        if not trips.empty:
            meta = db.query_df("SELECT ticker, open_ts FROM markets").set_index("ticker")["open_ts"].to_dict()
            age = [(en / NS - (meta.get(mk) or 0)) for en, mk in zip(trips["entry_ns"], trips["market"])]
            kept = trips[[a >= exclude_first_s for a in age]]
            out["exclude_first_s"] = exclude_first_s
            out["net_pnl_after_exclusion"] = float(kept["net"].sum())
            out["n_trades_after_exclusion"] = int(len(kept))
            out["approximation"] = "post-hoc removal of trades; path effects on later trades are ignored"
    return out
