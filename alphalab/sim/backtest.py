"""Backtester: replays recorded data through the shared TradingEngine.

Determinism: the event order is fully determined by the database contents and
``ReplaySpec``; order/fill ids are counters; there is no wall-clock or
unseeded randomness. ``result_hash`` (a hash of every fill) lets tests and
users verify that two runs are identical.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from alphalab.core.book_manager import BookManager
from alphalab.core.config import FeeConfig, FillConfig, RiskConfig, research_risk_config
from alphalab.core.fees import FeeModel, FeeSchedule
from alphalab.data.db import Database, dumps
from alphalab.execution.risk import RiskEngine
from alphalab.research.features import FeatureEngine
from alphalab.sim.broker import SimBroker
from alphalab.sim.engine import TradingEngine
from alphalab.sim.metrics import compute_metrics
from alphalab.sim.portfolio import Portfolio
from alphalab.sim.replay import ReplaySource, ReplaySpec
from alphalab import __version__

NS = 1_000_000_000


def build_fee_model(fees: FeeConfig, market_meta: Optional[Dict[str, Dict[str, Any]]] = None,
                    stress: float = 1.0) -> FeeModel:
    fm = FeeModel(FeeSchedule(taker_rate=fees.taker_rate, maker_rate=fees.maker_rate, rounding=fees.rounding),
                  stress_multiplier=stress)
    for m in (market_meta or {}).values():
        if m.get("fee_type") and m.get("series_ticker"):
            fm.register_series(m["series_ticker"], m["fee_type"], m.get("fee_multiplier"))
    for series, spec in (fees.series or {}).items():
        if "flat_per_contract" in spec:
            fm.series_overrides[series] = FeeSchedule(flat_per_contract=float(spec["flat_per_contract"]),
                                                      rounding=fees.rounding)
        else:
            fm.register_series(series, spec.get("fee_type"), spec.get("fee_multiplier"))
    return fm


@dataclass
class BacktestConfig:
    replay: ReplaySpec = field(default_factory=ReplaySpec)
    fill: FillConfig = field(default_factory=FillConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    fee_stress: float = 1.0
    risk: Optional[RiskConfig] = field(default_factory=research_risk_config)
    pnl_interval_s: float = 60.0
    record_signals: bool = False


@dataclass
class BacktestResult:
    run_id: str
    strategy: str
    params: Dict[str, Any]
    metrics: Dict[str, Any]
    fills: pd.DataFrame
    orders: pd.DataFrame
    trips: pd.DataFrame
    pnl: pd.DataFrame
    signals: List[Dict[str, Any]]
    risk_events: List[Dict[str, Any]]
    result_hash: str
    data_hash: str
    elapsed_s: float


def run_backtest(db: Database, strategy_factory: Callable[[], Any], cfg: BacktestConfig,
                 run_id: Optional[str] = None, persist: bool = True, persist_details: bool = True,
                 kind: str = "backtest", experiment_id: Optional[str] = None, phase: Optional[str] = None,
                 source: Optional[ReplaySource] = None) -> BacktestResult:
    t0 = time.time()
    strategy = strategy_factory()
    run_id = run_id or f"bt-{uuid.uuid4().hex[:12]}"
    src = source or ReplaySource(db, copy.deepcopy(cfg.replay))
    close_ns = src.market_close_ns()
    fees = build_fee_model(cfg.fees, src.meta, cfg.fee_stress)
    books = BookManager()
    portfolio = Portfolio()
    broker = SimBroker(books, fees, cfg.fill, portfolio, close_ns)
    risk_events: List[Dict[str, Any]] = []
    risk = None
    if cfg.risk is not None:
        risk = RiskEngine(cfg.risk, mode="backtest", market_close_ns=close_ns,
                          on_event=lambda t, s, d: risk_events.append({"type": t, "severity": s, **d}))
    features = FeatureEngine(market_meta=src.meta)
    engine = TradingEngine([strategy], broker, books, features, risk, src.meta, mode="backtest",
                           run_id=run_id, pnl_interval_s=cfg.pnl_interval_s, record_signals=cfg.record_signals)
    started = False
    for ev in src:
        if ev.ts_ns < src.start_ns:
            engine.warm(ev)
            continue
        if not started:
            engine.start(ev.ts_ns)
            started = True
        engine.handle(ev)
    if started:
        # let pending activations/cancels resolve, then stop
        last = engine.now_ns
        engine.advance_to(last + 10 * NS)
        engine.stop()
    marks = engine.marks()
    open_unreal = portfolio.unrealized(marks)
    mo = engine.markouts(broker.fills)
    metrics = compute_metrics(fills=broker.fills, trips=portfolio.trips, pnl_curve=engine.pnl_curve,
                              broker_stats=broker.stats, markouts=mo, final_equity=engine.equity(),
                              fees_paid=portfolio.fees_paid, open_unrealized=open_unreal,
                              traded_notional=portfolio.traded_notional,
                              open_positions=len(portfolio.open_positions()))
    metrics["risk_rejections"] = dict(risk.rejections) if risk else {}
    metrics["halted"] = bool(risk and risk.halted)
    metrics["events"] = engine.event_count
    fills_df = pd.DataFrame([asdict(f) for f in broker.fills])
    if not fills_df.empty:
        fills_df = fills_df.merge(pd.DataFrame(mo), on="fill_id", how="left")
    orders_df = pd.DataFrame([engine.order_row(o) for o in broker.orders.values()])
    trips_df = pd.DataFrame([dict(run_id=run_id, strategy=t.strategy, market=t.market, direction=t.direction,
                                  qty=t.qty, entry_ns=t.entry_ns, exit_ns=t.exit_ns, entry_price=t.entry_price,
                                  exit_price=t.exit_price, gross=t.gross, fees=t.fees, net=t.net,
                                  exit_reason=t.exit_reason) for t in portfolio.trips])
    pnl_df = pd.DataFrame(engine.pnl_curve)
    h = hashlib.sha256()
    for f in broker.fills:
        h.update(f"{f.ts_ns}|{f.market}|{f.action}|{f.price}|{f.qty:.6f}|{f.fee:.6f}|{f.liquidity};".encode())
    result_hash = h.hexdigest()[:16]
    data_hash = src.data_hash()
    res = BacktestResult(run_id, strategy.name, dict(strategy.params), metrics, fills_df, orders_df, trips_df,
                         pnl_df, engine.signals, risk_events, result_hash, data_hash, time.time() - t0)
    if persist:
        persist_result(db, res, cfg, kind, experiment_id, phase, details=persist_details)
    return res


def persist_result(db: Database, res: BacktestResult, cfg: BacktestConfig, kind: str,
                   experiment_id: Optional[str], phase: Optional[str], details: bool = True) -> None:
    db.execute("DELETE FROM runs WHERE run_id = ?", [res.run_id])
    db.insert_rows("runs", [dict(run_id=res.run_id, kind=kind, strategy=res.strategy, params=dumps(res.params),
                                 config=dumps({"fill": asdict(cfg.fill), "fees": asdict(cfg.fees),
                                               "fee_stress": cfg.fee_stress,
                                               "risk": asdict(cfg.risk) if cfg.risk else None}),
                                 data_spec=dumps(cfg.replay.to_dict()), data_hash=res.data_hash,
                                 created_ns=time.time_ns(), code_version=__version__, metrics=dumps(res.metrics),
                                 experiment_id=experiment_id, phase=phase, result_hash=res.result_hash)])
    if not details:
        return
    if not res.orders.empty:
        o = res.orders.copy()
        db.insert_df("orders", o)
    if not res.fills.empty:
        f = res.fills.copy()
        f["run_id"] = res.run_id
        f["mode"] = kind
        f["slippage_usd"] = [((1 if a == "buy" else -1) * (p - m) / 10000 * q) if m == m and m is not None else None
                             for a, p, m, q in zip(f["action"], f["price"], f["mid_at_fill"], f["qty"])]
        keep = ["run_id", "mode", "fill_id", "order_id", "strategy", "market", "ts_ns", "action", "price", "qty",
                "liquidity", "fee", "mid_at_fill", "slippage_usd", "position_after", "markout_1s", "markout_5s",
                "markout_30s", "markout_60s", "tag"]
        db.insert_df("fills", f[[c for c in keep if c in f.columns]])
    if not res.trips.empty:
        db.insert_df("round_trips", res.trips)
    if not res.pnl.empty:
        p = res.pnl.copy()
        p["positions"] = p["positions"].map(lambda d: json.dumps(d))
        db.insert_df("pnl", p)
    if res.signals:
        s = pd.DataFrame(res.signals)
        s["payload"] = s["payload"].map(lambda d: json.dumps(d, default=str))
        db.insert_df("signals", s)
    if res.risk_events:
        db.insert_rows("risk_events", [dict(ts_ns=e.get("ts_ns"), mode=kind, run_id=res.run_id, type=e["type"],
                                            severity=e["severity"], detail=dumps(e)) for e in res.risk_events[:5000]])
