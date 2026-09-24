"""Backtest / paper performance metrics.

P&L decomposition (all in dollars):

    mid_pnl         P&L had every fill executed at the prevailing mid
    exec_cost       what we paid versus mid (positive = cost; negative for
                    makers means spread captured)
    gross_pnl       = mid_pnl - exec_cost  (P&L at actual execution prices)
    fees            exchange fees under the configured fee model
    net_pnl         = gross_pnl - fees      <- the only number that matters

``net_pnl`` includes open positions marked to the last mid (reported
separately as ``open_unrealized``) and positions settled at their settlement
value. Trade statistics use *round trips* (flat -> position -> flat/settle).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional

from alphalab.core.prices import CENT, PRICE_SCALE

NS = 1_000_000_000


def _day(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / NS, timezone.utc).strftime("%Y-%m-%d")


def _safe(x: float) -> Optional[float]:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return float(x)


def max_drawdown(equity: List[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    return mdd


def compute_metrics(*, fills: List[Any], trips: List[Any], pnl_curve: List[Dict[str, Any]],
                    broker_stats: Dict[str, Any], markouts: List[Dict[str, Any]], final_equity: float,
                    fees_paid: float, open_unrealized: float, traded_notional: float,
                    open_positions: int) -> Dict[str, Any]:
    m: Dict[str, Any] = {}
    nets = [t.net for t in trips]
    n = len(nets)
    m["n_trades"] = n
    m["n_fills"] = len(fills)
    m["n_maker_fills"] = sum(1 for f in fills if f.liquidity == "maker")
    m["n_taker_fills"] = sum(1 for f in fills if f.liquidity == "taker")
    m["net_pnl"] = final_equity
    m["fees"] = fees_paid
    m["gross_pnl"] = final_equity + fees_paid
    exec_cost = 0.0
    for f in fills:
        if f.mid_at_fill is None:
            continue
        d = 1 if f.action == "buy" else -1
        exec_cost += d * (f.price - f.mid_at_fill) / PRICE_SCALE * f.qty
    m["exec_cost_vs_mid"] = exec_cost
    m["est_slippage"] = sum(d_cost for d_cost in [
        (1 if f.action == "buy" else -1) * (f.price - f.mid_at_fill) / PRICE_SCALE * f.qty
        for f in fills if f.liquidity == "taker" and f.mid_at_fill is not None])
    m["mid_pnl"] = m["gross_pnl"] + exec_cost
    m["open_unrealized"] = open_unrealized
    m["open_positions_at_end"] = open_positions
    if n:
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x < 0]
        mean = sum(nets) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (n - 1)) if n > 1 else 0.0
        m["win_rate"] = len(wins) / n
        m["avg_trade"] = mean
        m["expectancy"] = mean
        m["median_trade"] = median(nets)
        m["profit_factor"] = (sum(wins) / -sum(losses)) if losses else (math.inf if wins else None)
        m["sharpe_per_trade"] = mean / sd if sd > 0 else None
        m["t_stat"] = mean / (sd / math.sqrt(n)) if sd > 0 else None
        m["avg_trade_gross"] = sum(t.gross for t in trips) / n
        m["avg_trade_fees"] = sum(t.fees for t in trips) / n
        holds = [(t.exit_ns - t.entry_ns) / NS for t in trips if t.exit_ns and t.entry_ns]
        m["avg_holding_s"] = sum(holds) / len(holds) if holds else None
        m["exit_reasons"] = dict(_count(t.exit_reason for t in trips))
    else:
        for k in ("win_rate", "avg_trade", "expectancy", "median_trade", "profit_factor", "sharpe_per_trade",
                  "t_stat", "avg_trade_gross", "avg_trade_fees", "avg_holding_s"):
            m[k] = None
        m["exit_reasons"] = {}
    # per day / per market attribution (round-trip net by exit day)
    by_day: Dict[str, float] = defaultdict(float)
    by_mkt: Dict[str, float] = defaultdict(float)
    for t in trips:
        by_day[_day(t.exit_ns or t.entry_ns)] += t.net
        by_mkt[t.market] += t.net
    m["n_days"] = len(by_day)
    m["n_markets"] = len(by_mkt)
    m["pnl_by_day"] = dict(sorted(by_day.items()))
    m["pnl_by_market"] = dict(sorted(by_mkt.items(), key=lambda kv: -abs(kv[1]))[:50])
    tot_pos = sum(v for v in by_mkt.values() if v > 0)
    m["top_market_share"] = (max(by_mkt.values()) / tot_pos) if tot_pos > 0 else None
    tot_pos_d = sum(v for v in by_day.values() if v > 0)
    m["top_day_share"] = (max(by_day.values()) / tot_pos_d) if tot_pos_d > 0 else None
    days = list(by_day.values())
    if len(days) >= 5:
        mu = sum(days) / len(days)
        sd = math.sqrt(sum((x - mu) ** 2 for x in days) / (len(days) - 1))
        m["sharpe_daily_ann"] = (mu / sd * math.sqrt(365)) if sd > 0 else None
    else:
        m["sharpe_daily_ann"] = None
    eq = [r["equity"] for r in pnl_curve] or [0.0]
    m["max_drawdown"] = max_drawdown([0.0] + eq)
    exp = [r["gross_exposure"] for r in pnl_curve]
    m["avg_exposure"] = sum(exp) / len(exp) if exp else 0.0
    m["max_exposure"] = max(exp) if exp else 0.0
    m["traded_notional"] = traded_notional
    m["turnover"] = traded_notional / m["max_exposure"] if m["max_exposure"] > 0 else None
    inv = [abs(q) for r in pnl_curve for q in (r.get("positions") or {}).values()]
    m["avg_abs_inventory"] = sum(inv) / len(pnl_curve) if pnl_curve else 0.0
    m["max_abs_inventory"] = max(inv) if inv else 0.0
    sub = broker_stats.get("submitted_qty") or 0.0
    m["orders_submitted"] = broker_stats.get("submitted", 0)
    m["orders_rejected"] = broker_stats.get("rejected", 0)
    m["fill_rate"] = (broker_stats.get("filled_qty", 0.0) / sub) if sub > 0 else None
    # adverse selection: qty-weighted markout in cents/contract (positive = favourable)
    by_id = {f.fill_id: f for f in fills}
    for h in (1, 5, 30, 60):
        for label, filt in (("all", None), ("maker", "maker"), ("taker", "taker")):
            num = den = 0.0
            for mo in markouts:
                v = mo.get(f"markout_{h}s")
                f = by_id.get(mo["fill_id"])
                if v is None or f is None or (filt and f.liquidity != filt):
                    continue
                num += v * f.qty
                den += f.qty
            m[f"markout_{h}s_{label}_c"] = (num / den * 100.0) if den > 0 else None
    m["adverse_selection_c"] = (-m["markout_30s_all_c"]) if m.get("markout_30s_all_c") is not None else None
    return {k: (_safe(v) if isinstance(v, float) else v) for k, v in m.items()}


def _count(it):
    d: Dict[str, int] = defaultdict(int)
    for x in it:
        d[x or ""] += 1
    return d
