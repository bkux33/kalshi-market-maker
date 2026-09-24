"""Experiment engine: bounded parameter search with out-of-sample validation.

Protocol (all splits are chronological; nothing is shuffled):

1. Universe: markets (or a time range) ordered by close time.
2. Holdout: the last ``1 - train_frac - validation_frac`` of the universe is
   locked away as the TEST set. It is evaluated at most once per experiment
   and only if the candidate already passed every other gate.
3. Development set: the rest, cut into ``walk_forward_folds + 1`` contiguous
   blocks. Every grid point is backtested on every block (grid size is capped
   at ``max_grid_size``; the engine refuses larger searches).
4. Walk-forward: for fold k, parameters are selected on blocks 0..k-1 only and
   evaluated on block k. Concatenated fold results are the out-of-sample (OOS)
   record. The last fold doubles as the validation set.
5. For the final parameters (selected on all development blocks except the
   last): cost stress (2x fees, +1 tick slippage, +250ms latency, conservative
   queue), parameter-neighbour sensitivity, a random-direction benchmark at the
   same entry times (taker strategies), and a probabilistic Sharpe ratio
   deflated for the number of grid points tried.
6. Classification by ``research.validation`` rules; results persisted.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from alphalab.core.config import FeeConfig, FillConfig, ValidationConfig, research_risk_config
from alphalab.data.db import Database, dumps
from alphalab.research import validation as V
from alphalab.sim.backtest import BacktestConfig, BacktestResult, run_backtest
from alphalab.sim.metrics import compute_metrics
from alphalab.sim.replay import ReplaySource, ReplaySpec
from alphalab.strategies import get_strategy
from alphalab.strategies.base import param_count
from alphalab.strategies.common import TakerHorizonStrategy

log = logging.getLogger(__name__)
NS = 1_000_000_000


@dataclass
class ExperimentSpec:
    name: str
    strategy: str
    base_params: Dict[str, Any] = field(default_factory=dict)
    grid: Dict[str, List[Any]] = field(default_factory=dict)
    markets: Optional[List[str]] = None
    series: Optional[List[str]] = None
    split_by: str = "auto"                  # auto | market | time
    fill: FillConfig = field(default_factory=FillConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    random_trials: Optional[int] = None     # default: validation.random_benchmark_trials
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Block:
    name: str
    markets: List[str]
    start_ns: Optional[int] = None
    end_ns: Optional[int] = None

    def replay(self) -> ReplaySpec:
        return ReplaySpec(markets=list(self.markets), start_ns=self.start_ns, end_ns=self.end_ns)


@dataclass
class ExperimentReport:
    experiment_id: str
    spec: ExperimentSpec
    split_by: str
    blocks: List[Block]
    test_block: Optional[Block]
    grid_points: List[Dict[str, Any]]
    selected_params: Dict[str, Any]
    is_metrics: Dict[str, Any]
    oos_metrics: Dict[str, Any]
    validation_metrics: Dict[str, Any]
    test_metrics: Optional[Dict[str, Any]]
    wf_folds: List[Dict[str, Any]]
    stress: Dict[str, Optional[Dict[str, Any]]]
    sensitivity: Optional[Dict[str, Any]]
    random_benchmark: Optional[Dict[str, Any]]
    psr: Optional[float]
    flags: List[Dict[str, Any]]
    classification: str
    reason: str
    data_quality: Dict[str, Any]
    elapsed_s: float
    grid_table: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------- helpers
def expand_grid(base: Dict[str, Any], grid: Dict[str, List[Any]], max_size: int) -> List[Dict[str, Any]]:
    keys = sorted(grid)
    size = 1
    for k in keys:
        size *= max(1, len(grid[k]))
    if size > max_size:
        raise ValueError(f"grid has {size} points > max_grid_size {max_size}; reduce the search space "
                         "(brute-forcing many combinations is how backtests get overfit)")
    points = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        p = dict(base)
        p.update(dict(zip(keys, combo)))
        points.append(p)
    return points or [dict(base)]


def _universe(db: Database, spec: ExperimentSpec) -> pd.DataFrame:
    df = db.query_df("""
        SELECT t.market, MIN(t.ts_ns) first_ns, MAX(t.ts_ns) last_ns, COUNT(*) n_tob,
               ANY_VALUE(m.close_ts) close_ts, ANY_VALUE(m.depth_quality) depth_quality,
               ANY_VALUE(m.source) AS src
        FROM tob t LEFT JOIN markets m ON m.ticker = t.market
        GROUP BY t.market""")
    if spec.markets:
        df = df[df["market"].isin(spec.markets)]
    if spec.series:
        df = df[df["market"].map(lambda t: t.split("-")[0] in spec.series)]
    df = df.copy()
    df["order_ts"] = df["close_ts"].fillna(df["last_ns"] / NS)
    return df.sort_values(["order_ts", "market"]).reset_index(drop=True)


def make_blocks(uni: pd.DataFrame, cfg: ValidationConfig, split_by: str) -> Tuple[str, List[Block], Optional[Block]]:
    n_dev_blocks = cfg.walk_forward_folds + 1
    test_frac = max(0.0, 1.0 - cfg.train_frac - cfg.validation_frac)
    markets = uni["market"].tolist()
    if split_by == "auto":
        split_by = "market" if len(markets) >= 2 * (n_dev_blocks + 1) else "time"
    if split_by == "market":
        n_test = int(round(len(markets) * test_frac))
        dev, test = markets[: len(markets) - n_test], markets[len(markets) - n_test:]
        size = len(dev) / n_dev_blocks
        blocks = [Block(f"dev{k}", dev[int(round(k * size)): int(round((k + 1) * size))]) for k in range(n_dev_blocks)]
        blocks = [b for b in blocks if b.markets]
        return split_by, blocks, (Block("test", test) if test else None)
    lo, hi = int(uni["first_ns"].min()), int(uni["last_ns"].max()) + 1
    t_test = int(hi - (hi - lo) * test_frac)
    edges = [int(lo + (t_test - lo) * k / n_dev_blocks) for k in range(n_dev_blocks + 1)]
    blocks = [Block(f"dev{k}", markets, edges[k], edges[k + 1]) for k in range(n_dev_blocks)]
    test = Block("test", markets, t_test, hi) if test_frac > 0 else None
    return split_by, blocks, test


def aggregate(results: List[BacktestResult]) -> Dict[str, Any]:
    """Combine chronological block results into one metrics dict."""
    results = [r for r in results if r is not None]
    fills, trips, curve, marks = [], [], [], []
    stats = {"submitted": 0, "rejected": 0, "submitted_qty": 0.0, "filled_qty": 0.0}
    offset = 0.0
    fees = unreal = notional = 0.0
    open_pos = 0
    for r in results:
        for row in r.fills.to_dict("records") if not r.fills.empty else []:
            fills.append(SimpleNamespace(**row))
            marks.append({k: row.get(k) for k in ("fill_id", "markout_1s", "markout_5s", "markout_30s", "markout_60s")})
        for row in r.trips.to_dict("records") if not r.trips.empty else []:
            trips.append(SimpleNamespace(**row))
        for row in r.pnl.to_dict("records") if not r.pnl.empty else []:
            row = dict(row)
            row["equity"] = row["equity"] + offset
            curve.append(row)
        offset += r.metrics.get("net_pnl") or 0.0
        fees += r.metrics.get("fees") or 0.0
        unreal += r.metrics.get("open_unrealized") or 0.0
        notional += r.metrics.get("traded_notional") or 0.0
        open_pos += r.metrics.get("open_positions_at_end") or 0
        stats["submitted"] += r.metrics.get("orders_submitted") or 0
        stats["rejected"] += r.metrics.get("orders_rejected") or 0
        sub = r.metrics.get("orders_submitted") or 0
        fr = r.metrics.get("fill_rate")
        # recover qty-weighted fill rate from per-run components
        stats["submitted_qty"] += float(r.orders["qty"].sum()) if not r.orders.empty else 0.0
        stats["filled_qty"] += float(r.orders["filled_qty"].sum()) if not r.orders.empty else 0.0
    # make fill ids unique across blocks for markout joins
    for i, (f, mk) in enumerate(zip(fills, marks)):
        f.fill_id = mk["fill_id"] = f"{i}:{f.fill_id}"
    m = compute_metrics(fills=fills, trips=trips, pnl_curve=curve, broker_stats=stats, markouts=marks,
                        final_equity=offset, fees_paid=fees, open_unrealized=unreal, traded_notional=notional,
                        open_positions=open_pos)
    m["n_blocks"] = len(results)
    return m


def _neighbors(selected: Dict[str, Any], grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    out = []
    for k, vals in grid.items():
        if len(vals) < 2 or selected.get(k) not in vals:
            continue
        i = vals.index(selected[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                p = dict(selected)
                p[k] = vals[j]
                out.append(p)
    return out


def _pkey(p: Dict[str, Any]) -> str:
    return json.dumps(p, sort_keys=True, default=str)


def strategy_key(strategy: str, params: Dict[str, Any]) -> str:
    return f"{strategy}:{_pkey(params)}"


def _objective(m: Dict[str, Any], min_trades: int) -> float:
    if (m.get("n_trades") or 0) < min_trades:
        return -math.inf
    return m.get("net_pnl") or 0.0


def _summary(m: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if m is None:
        return None
    keep = ("n_trades", "n_fills", "gross_pnl", "fees", "est_slippage", "exec_cost_vs_mid", "mid_pnl", "net_pnl",
            "expectancy", "win_rate", "median_trade", "profit_factor", "sharpe_per_trade", "sharpe_daily_ann",
            "t_stat", "max_drawdown", "avg_exposure", "max_exposure", "turnover", "traded_notional",
            "avg_holding_s", "fill_rate", "adverse_selection_c", "markout_5s_all_c", "markout_30s_all_c",
            "markout_30s_maker_c", "avg_abs_inventory", "max_abs_inventory", "n_days", "n_markets",
            "top_market_share", "top_day_share", "open_positions_at_end", "open_unrealized", "n_maker_fills",
            "n_taker_fills")
    return {k: m.get(k) for k in keep}


# ---------------------------------------------------------------------- main
def run_experiment(db: Database, spec: ExperimentSpec, persist: bool = True,
                   progress: Optional[Callable[[str], None]] = None) -> ExperimentReport:
    t0 = time.time()
    say = progress or (lambda s: log.info(s))
    cfg = spec.validation
    cls = get_strategy(spec.strategy)
    exp_id = f"exp-{uuid.uuid4().hex[:10]}"
    points = expand_grid(spec.base_params, spec.grid, cfg.max_grid_size)
    for p in points:
        cls(**p)  # validate parameter names early
    uni = _universe(db, spec)
    if uni.empty:
        raise ValueError("no market data matches the experiment universe; record or import data first")
    split_by, blocks, test_block = make_blocks(uni, cfg, spec.split_by)
    if len(blocks) < 2:
        raise ValueError("not enough data for walk-forward validation (need >= 2 development blocks)")
    say(f"[{exp_id}] {spec.name}: {len(points)} grid points x {len(blocks)} blocks ({split_by} split), "
        f"{len(uni)} markets")
    uni_markets = set(uni["market"])
    dq_rows = db.query_df("SELECT COUNT(*) n FROM settlements WHERE inferred")
    data_quality = {
        "inferred_settlements": int(db.query_df(
            f"SELECT COUNT(*) n FROM settlements WHERE inferred AND market IN ({','.join('?' for _ in uni_markets)})",
            list(uni_markets))["n"].iloc[0]) if uni_markets else 0,
        "assumed_depth": bool(uni["depth_quality"].fillna("").str.startswith("top_only").any()),
        "synthetic": bool(uni["depth_quality"].fillna("").eq("synthetic").any()),
        "n_markets": len(uni),
    }
    risk = research_risk_config()
    sources: Dict[str, ReplaySource] = {}

    def source_for(block: Block) -> ReplaySource:
        if block.name not in sources:
            sources[block.name] = ReplaySource(db, block.replay()).materialize()
        return sources[block.name]

    def bt(params: Dict[str, Any], block: Block, fill: FillConfig = spec.fill, fee_stress: float = 1.0,
           phase: str = "", persist_run: bool = False, strategy_name: Optional[str] = None) -> BacktestResult:
        scls = get_strategy(strategy_name) if strategy_name else cls
        bcfg = BacktestConfig(replay=block.replay(), fill=fill, fees=spec.fees, fee_stress=fee_stress, risk=risk)
        return run_backtest(db, lambda: scls(**params), bcfg, persist=persist and persist_run,
                            persist_details=True, kind="experiment", experiment_id=exp_id, phase=phase,
                            source=source_for(block))

    # --- grid on every development block
    grid_res: Dict[Tuple[str, int], BacktestResult] = {}
    for i, p in enumerate(points):
        for k, b in enumerate(blocks):
            grid_res[(_pkey(p), k)] = bt(p, b)
        say(f"  grid {i + 1}/{len(points)} done")

    def agg_for(p: Dict[str, Any], ks: List[int]) -> Dict[str, Any]:
        return aggregate([grid_res[(_pkey(p), k)] for k in ks])

    def select(ks: List[int]) -> Dict[str, Any]:
        best, best_v = points[0], -math.inf
        for p in points:
            v = _objective(agg_for(p, ks), cfg.min_trades_research // max(1, len(blocks)))
            if v > best_v:
                best, best_v = p, v
        return best

    # --- walk-forward
    wf, wf_results = [], []
    for k in range(1, len(blocks)):
        sel = select(list(range(k)))
        r = grid_res[(_pkey(sel), k)]
        wf_results.append(r)
        wf.append({"fold": k, "train_blocks": list(range(k)), "test_block": k, "params": sel,
                   "net_pnl": r.metrics.get("net_pnl"), "n_trades": r.metrics.get("n_trades"),
                   "expectancy": r.metrics.get("expectancy")})
    oos = aggregate(wf_results)
    last = len(blocks) - 1
    selected = select(list(range(last)))
    is_m = agg_for(selected, list(range(last)))
    val_res = grid_res[(_pkey(selected), last)]
    val_m = val_res.metrics
    say(f"  walk-forward OOS: trades={oos.get('n_trades')} net={oos.get('net_pnl'):.2f}")

    # --- stress on the OOS blocks with the final parameters
    oos_blocks = [blocks[k] for k in range(1, len(blocks))]
    stress_defs = {
        "fees_x2": dict(fee_stress=cfg.stress_fee_multiplier),
        "slippage_plus_1tick": dict(fill=replace(spec.fill, taker_slippage_ticks=spec.fill.taker_slippage_ticks
                                                 + cfg.stress_slippage_ticks)),
        "latency_plus_250ms": dict(fill=replace(spec.fill, order_latency_ms=spec.fill.order_latency_ms
                                                + cfg.stress_latency_ms,
                                                cancel_latency_ms=spec.fill.cancel_latency_ms + cfg.stress_latency_ms)),
        "conservative_queue": dict(fill=replace(spec.fill, queue_cancel_model="back", allow_touch_fill=False)),
    }
    base_sel_oos = aggregate([grid_res[(_pkey(selected), k)] for k in range(1, len(blocks))])
    stress: Dict[str, Optional[Dict[str, Any]]] = {}
    for name, kw in stress_defs.items():
        stress[name] = _summary(aggregate([bt(selected, b, **kw) for b in oos_blocks]))
    # --- sensitivity on the validation block
    neigh = _neighbors(selected, spec.grid)
    sensitivity = None
    if neigh:
        nets = [grid_res[(_pkey(p), last)].metrics.get("net_pnl") or 0.0 for p in neigh]
        base_net = val_m.get("net_pnl") or 0.0
        sensitivity = {"n_neighbors": len(neigh), "positive_neighbor_frac": sum(1 for x in nets if x > 0) / len(nets),
                       "neighbor_nets": nets, "selected_net": base_net,
                       "mean_neighbor_net": sum(nets) / len(nets)}
    # --- random-direction benchmark (taker strategies)
    random_bench = None
    pvalue = None
    if issubclass(cls, TakerHorizonStrategy) and cls.name != "random_entry":
        trials = spec.random_trials if spec.random_trials is not None else cfg.random_benchmark_trials
        entry_times = []
        for r in wf_results:
            if not r.orders.empty:
                e = r.orders[(r.orders["tag"] == "entry") & (r.orders["filled_qty"] > 0)]
                entry_times += [[m, int(ts)] for m, ts in zip(e["market"], e["ts_decision"])]
        if entry_times and trials > 0:
            common = {k: selected[k] for k in TakerHorizonStrategy.common_params() if k in selected}
            common["cooldown_s"] = 0.0
            strat_net = oos.get("net_pnl") or 0.0
            nets = []
            for seed in range(trials):
                res = [bt({**common, "entry_times": [x for x in entry_times if x[0] in set(b.markets)], "seed": seed},
                          b, strategy_name="random_entry") for b in oos_blocks]
                nets.append(sum(r.metrics.get("net_pnl") or 0.0 for r in res))
            ge = sum(1 for x in nets if x >= strat_net)
            pvalue = (1 + ge) / (1 + len(nets))
            random_bench = {"trials": len(nets), "strategy_net": strat_net, "random_mean_net": sum(nets) / len(nets),
                            "random_p95_net": sorted(nets)[int(0.95 * (len(nets) - 1))], "pvalue": pvalue,
                            "n_entries": len(entry_times)}
            say(f"  random benchmark: p={pvalue:.3f}")
    # --- probabilistic Sharpe deflated by the number of trials
    oos_trade_nets = [t.net for r in wf_results for t in (r.trips.itertuples() if not r.trips.empty else [])]
    train_sharpes = [agg_for(p, list(range(last))).get("sharpe_per_trade") for p in points] if len(points) > 1 else []
    sr_star = V.expected_max_sharpe([s for s in train_sharpes if s is not None]) if train_sharpes else 0.0
    psr = V.probabilistic_sharpe(oos_trade_nets, sr_star) if oos_trade_nets else None
    # --- flags, classification, and (only if eligible) the holdout test
    n_params = param_count(cls, spec.grid)
    flags = V.compute_flags(oos=oos, is_metrics=is_m, test=None, stress=stress, sensitivity=sensitivity,
                            wf_fold_nets=[w["net_pnl"] or 0.0 for w in wf], pvalue=pvalue, psr=psr,
                            n_params=n_params, data_quality=data_quality, cfg=cfg,
                            benchmark_trials=random_bench["trials"] if random_bench else None)
    status, reason = V.classify(oos, flags, test=None, cfg=cfg)
    test_m = None
    if test_block is not None and status == V.RESEARCH and reason == "holdout test not evaluated":
        prior = db.query_df("SELECT COALESCE(SUM(test_evaluations), 0) n FROM experiments WHERE strategy = ?",
                            [spec.strategy])["n"].iloc[0]
        test_res = bt(selected, test_block, phase="test", persist_run=True)
        test_m = test_res.metrics
        if prior and int(prior) > 0:
            flags.append({"code": "holdout_reused", "gating": True,
                          "message": f"test set already evaluated {int(prior)} time(s) for this strategy"})
        flags = [f for f in flags] + [x for x in V.compute_flags(
            oos=oos, is_metrics=None, test=test_m, stress={}, sensitivity=None, wf_fold_nets=[], pvalue=0.0,
            psr=None, n_params=0, data_quality={}, cfg=cfg) if x["code"] == "fails_holdout"]
        status, reason = V.classify(oos, flags, test=test_m, cfg=cfg)
    report = ExperimentReport(
        experiment_id=exp_id, spec=spec, split_by=split_by, blocks=blocks, test_block=test_block,
        grid_points=points, selected_params=selected, is_metrics=_summary(is_m), oos_metrics=_summary(oos),
        validation_metrics=_summary(val_m), test_metrics=_summary(test_m), wf_folds=wf, stress=stress,
        sensitivity=sensitivity, random_benchmark=random_bench, psr=psr, flags=flags, classification=status,
        reason=reason, data_quality=data_quality, elapsed_s=time.time() - t0,
        grid_table=[{"params": p, **{k: agg_for(p, list(range(last))).get(k)
                                     for k in ("n_trades", "net_pnl", "expectancy", "sharpe_per_trade")}}
                    for p in points])
    if persist:
        persist_experiment(db, report, grid_res, blocks, wf_results)
    say(f"[{exp_id}] classification: {status} - {reason}")
    return report


def persist_experiment(db: Database, rep: ExperimentReport, grid_res: Dict[Tuple[str, int], BacktestResult],
                       blocks: List[Block], wf_results: List[BacktestResult]) -> None:
    summary = {
        "split_by": rep.split_by, "is": rep.is_metrics, "oos": rep.oos_metrics, "validation": rep.validation_metrics,
        "test": rep.test_metrics, "wf_folds": rep.wf_folds, "stress": rep.stress, "sensitivity": rep.sensitivity,
        "random_benchmark": rep.random_benchmark, "psr": rep.psr, "data_quality": rep.data_quality,
        "blocks": [{"name": b.name, "n_markets": len(b.markets), "start_ns": b.start_ns, "end_ns": b.end_ns,
                    "markets": b.markets[:200]} for b in rep.blocks],
        "test_block": None if rep.test_block is None else {"n_markets": len(rep.test_block.markets),
                                                          "start_ns": rep.test_block.start_ns,
                                                          "end_ns": rep.test_block.end_ns},
        "reason": rep.reason, "elapsed_s": rep.elapsed_s, "grid_table": rep.grid_table,
    }
    spec_d = rep.spec.to_dict()
    db.insert_rows("experiments", [dict(
        experiment_id=rep.experiment_id, name=rep.spec.name, strategy=rep.spec.strategy, created_ns=time.time_ns(),
        spec=dumps(spec_d), status="complete", classification=rep.classification, flags=dumps(rep.flags),
        summary=dumps(summary), selected_params=dumps(rep.selected_params),
        test_evaluations=1 if rep.test_metrics is not None else 0,
        data_hash=wf_results[0].data_hash if wf_results else "", n_trials=len(rep.grid_points))])
    rows = []
    for (pk, k), r in grid_res.items():
        rows.append(dict(experiment_id=rep.experiment_id, run_id=r.run_id, phase=f"dev{k}", fold=k, params=pk,
                         metrics=dumps(_summary(r.metrics))))
    db.insert_rows("experiment_results", rows)
    # persist full detail for the walk-forward OOS runs (auditable fills/orders)
    from alphalab.sim.backtest import persist_result
    for i, r in enumerate(wf_results):
        persist_result(db, r, BacktestConfig(replay=blocks[i + 1].replay(), fill=rep.spec.fill, fees=rep.spec.fees),
                       "experiment", rep.experiment_id, f"wf_oos{i + 1}", details=True)
    key = strategy_key(rep.spec.strategy, rep.selected_params)
    db.execute("DELETE FROM strategy_status WHERE strategy_key = ?", [key])
    db.insert_rows("strategy_status", [dict(strategy_key=key, strategy=rep.spec.strategy,
                                            params=dumps(rep.selected_params), status=rep.classification,
                                            experiment_id=rep.experiment_id, updated_ns=time.time_ns(),
                                            reason=rep.reason)])
