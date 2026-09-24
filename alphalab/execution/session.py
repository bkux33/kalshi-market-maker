"""Trading session used by paper and live trading (and replay-driven paper).

The session wraps the same ``TradingEngine`` the backtester uses, adding the
things that only exist in real time: wall-clock timers, stale-data and
disconnect handling, the latched kill switch, a JSONL journal, and a state
file the dashboard reads.

Drivers:
* ``run_live_feed``   - Kalshi WebSocket feed (paper or live broker)
* ``run_replay_feed`` - recorded data from DuckDB, clock = event time
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from alphalab import __version__
from alphalab.core.book_manager import BookManager
from alphalab.core.config import Settings
from alphalab.core.events import Settlement
from alphalab.core.fees import FeeModel
from alphalab.core.logs import log_event
from alphalab.execution.journal import Journal
from alphalab.execution.killswitch import KillSwitch
from alphalab.execution.risk import RiskEngine
from alphalab.research.features import FeatureEngine
from alphalab.sim.backtest import build_fee_model
from alphalab.sim.broker import SimBroker
from alphalab.sim.engine import TradingEngine
from alphalab.sim.metrics import compute_metrics
from alphalab.sim.portfolio import Portfolio
from alphalab.strategies.base import Strategy

log = logging.getLogger(__name__)
NS = 1_000_000_000


class TradingSession:
    def __init__(self, settings: Settings, strategies: List[Strategy], mode: str = "paper",
                 market_meta: Optional[Dict[str, Dict[str, Any]]] = None, broker_factory=None,
                 session_id: Optional[str] = None):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        self.settings = settings
        self.mode = mode
        self.run_id = session_id or f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.meta: Dict[str, Dict[str, Any]] = dict(market_meta or {})
        self.kill = KillSwitch(settings.risk.kill_switch_file)
        self.fees: FeeModel = build_fee_model(settings.fees, self.meta)
        self.books = BookManager()
        self.portfolio = Portfolio()
        close_ns = {m: int(v["close_ts"] * NS) for m, v in self.meta.items() if v.get("close_ts")}
        jdir = settings.data.journal_dir
        self._journal_path = Path(jdir) / f"{self.run_id}.jsonl.active"
        self.journal = Journal(self._journal_path)
        self.risk = RiskEngine(settings.risk, mode=mode, kill_switch=self.kill, market_close_ns=close_ns,
                               on_event=self._on_risk)
        if broker_factory is None:
            self.broker = SimBroker(self.books, self.fees, settings.fill, self.portfolio, close_ns)
        else:
            self.broker = broker_factory(self.books, self.fees, self.portfolio, close_ns)
        self.features = FeatureEngine(market_meta=self.meta)
        self.engine = TradingEngine(strategies, self.broker, self.books, self.features, self.risk, self.meta,
                                    mode=mode, run_id=self.run_id, pnl_interval_s=10.0, record_signals=True,
                                    on_record=self._record)
        self.strategies = strategies
        self.state_path = Path(settings.data.state_dir) / f"{mode}_state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_state = 0.0
        self._kill_handled = False
        self._started = False
        self._markout_done: set = set()
        self.recent: Dict[str, List[Dict[str, Any]]] = {"fills": [], "signals": [], "risk": [], "orders": []}
        self.journal.write("session", {"run_id": self.run_id, "mode": mode, "started_ns": time.time_ns(),
                                       "strategy": ",".join(s.name for s in strategies),
                                       "params": {s.name: s.params for s in strategies},
                                       "config": settings.redacted(), "markets": sorted(self.meta),
                                       "version": __version__})
        log_event(log, "session_start", mode=mode, run_id=self.run_id, strategies=[s.key for s in strategies])

    # ------------------------------------------------------------------ records
    def _record(self, kind: str, row: Dict[str, Any]) -> None:
        self.journal.write(kind, row)
        if kind == "fill":
            log_event(log, "fill", **{k: row.get(k) for k in ("market", "action", "price", "qty", "liquidity", "fee",
                                                              "strategy", "order_id")})
            self._push("fills", row)
        elif kind in ("order", "order_submitted"):
            log_event(log, "order" if kind == "order_submitted" else "order_update",
                      **{k: row.get(k) for k in ("order_id", "market", "action", "price", "qty", "status", "reason")})
            self._push("orders", row)
        elif kind == "signal":
            self._push("signals", row)

    def _push(self, key: str, row: Dict[str, Any], keep: int = 200) -> None:
        lst = self.recent[key]
        lst.append(row)
        if len(lst) > keep:
            del lst[: len(lst) - keep]

    def _on_risk(self, typ: str, severity: str, detail: Dict[str, Any]) -> None:
        ts = detail.get("ts_ns")
        if ts is None:
            ts = self.engine.now_ns if hasattr(self, "engine") else time.time_ns()
        row = {"run_id": self.run_id, "mode": self.mode, "type": typ, "severity": severity, **detail, "ts_ns": ts}
        self.journal.write("risk", row)
        self._push("risk", row)

    # ------------------------------------------------------------------ market registry
    def register_market(self, ticker: str, meta: Dict[str, Any]) -> None:
        self.meta[ticker] = meta
        if meta.get("close_ts"):
            cn = int(meta["close_ts"] * NS)
            self.risk.market_close_ns[ticker] = cn
            self.broker.market_close_ns[ticker] = cn
        if meta.get("fee_type") and meta.get("series_ticker"):
            self.fees.register_series(meta["series_ticker"], meta["fee_type"], meta.get("fee_multiplier"))

    # ------------------------------------------------------------------ event entry points
    def on_event(self, ev: Any) -> None:
        if not self._started:
            self.engine.start(ev.ts_ns)
            self._started = True
        if getattr(ev, "market", None) and ev.kind != "external" and ev.market not in self.meta:
            self.register_market(ev.market, {"ticker": ev.market})
        self.engine.handle(ev)

    def on_connection(self, connected: bool, now_ns: Optional[int] = None) -> None:
        now_ns = now_ns or time.time_ns()
        self.risk.on_connection(connected, now_ns)
        if not connected and self.settings.risk.on_disconnect == "cancel_all":
            n = self.broker.cancel_all(max(now_ns, self.engine.now_ns))
            log_event(log, "disconnect_cancel_all", level=logging.WARNING, cancelled=n)

    def tick(self, now_ns: Optional[int] = None) -> None:
        now_ns = now_ns or time.time_ns()
        if not self._started:
            return
        self.engine.advance_to(max(now_ns, self.engine.now_ns))
        now = self.engine.now_ns
        if self.kill.is_tripped():
            if not self._kill_handled:
                self._kill_handled = True
                n = self.broker.cancel_all(now)
                self.risk.halt("kill_switch", now)
                log_event(log, "kill_switch", level=logging.CRITICAL, cancelled=n, info=self.kill.info())
        stale = self.risk.stale_markets(now, {o.market for o in self.broker.open_orders()})
        for m in stale:
            n = self.broker.cancel_all(now, market=m)
            if n:
                self._on_risk("stale_data_cancel", "warning", {"market": m, "cancelled": n, "ts_ns": now})
        self._markouts(now)
        if time.monotonic() - self._last_state >= 1.0:
            self.write_state()
            self._last_state = time.monotonic()

    def _markouts(self, now: int) -> None:
        ready = [f for f in self.broker.fills if f.fill_id not in self._markout_done and now - f.ts_ns > 61 * NS]
        for row in self.engine.markouts(ready):
            self.journal.write("markout", row)
            self._markout_done.add(row["fill_id"])

    # ------------------------------------------------------------------ reporting
    def metrics(self) -> Dict[str, Any]:
        mo = self.engine.markouts([f for f in self.broker.fills if f.fill_id in self._markout_done])
        return compute_metrics(fills=self.broker.fills, trips=self.portfolio.trips, pnl_curve=self.engine.pnl_curve,
                               broker_stats=getattr(self.broker, "stats", {}), markouts=mo,
                               final_equity=self.engine.equity(), fees_paid=self.portfolio.fees_paid,
                               open_unrealized=self.portfolio.unrealized(self.engine.marks()),
                               traded_notional=self.portfolio.traded_notional,
                               open_positions=len(self.portfolio.open_positions()))

    def state(self) -> Dict[str, Any]:
        marks = self.engine.marks()
        books = {}
        for m, ob in list(self.books.books.items())[:200]:
            if self.books.is_synced(m):
                books[m] = ob.to_dict(levels=10)
        m = self.metrics()
        return {
            "run_id": self.run_id, "mode": self.mode, "updated": time.time(), "now_ns": self.engine.now_ns,
            "strategies": [{"name": s.name, "key": s.key, "params": s.params} for s in self.strategies],
            "risk": self.risk.snapshot(), "kill_switch": self.kill.info(),
            "positions": [{"strategy": s, "market": mk, "qty": p.qty, "avg_price": p.avg_price,
                           "mark": marks.get(mk), "unrealized": (marks.get(mk, p.avg_price) - p.avg_price) * p.qty}
                          for (s, mk), p in self.portfolio.open_positions().items()],
            "open_orders": [self.engine.order_row(o) for o in self.broker.open_orders()],
            "pnl": {"equity": self.engine.equity(), "realized": self.portfolio.realized(),
                    "unrealized": self.portfolio.unrealized(marks), "fees": self.portfolio.fees_paid,
                    "curve": self.engine.pnl_curve[-500:]},
            "metrics": m, "books": books, "recent": self.recent,
        }

    def write_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state(), default=str))
        os.replace(tmp, self.state_path)

    def stop(self, reason: str = "stop") -> Dict[str, Any]:
        now = max(time.time_ns(), self.engine.now_ns) if self.mode == "live" else self.engine.now_ns
        self.broker.cancel_all(now)
        if self._started:
            self.engine.advance_to(now + 5 * NS)
            self.engine.stop()
        m = self.metrics()
        self.journal.write("metrics", m)
        for row in self.engine.pnl_curve:
            self.journal.write("pnl", row)
        self._markouts(self.engine.now_ns + 120 * NS)
        self.write_state()
        self.journal.close()
        final = self._journal_path.with_name(self._journal_path.name.replace(".active", ""))
        os.replace(self._journal_path, final)
        log_event(log, "session_stop", run_id=self.run_id, reason=reason, net_pnl=m.get("net_pnl"),
                  fills=m.get("n_fills"))
        return m


# ---------------------------------------------------------------------- drivers
def run_replay_feed(session: TradingSession, source) -> Dict[str, Any]:
    """Drive a session with recorded events; the clock is event time."""
    for ev in source:
        if ev.ts_ns < getattr(source, "start_ns", 0):
            session.engine.warm(ev)
            continue
        session.tick(ev.ts_ns)
        session.on_event(ev)
    return session.stop("replay_complete")


async def run_live_feed(session: TradingSession, rest, ws_url: str, signer, series: List[str],
                        markets: List[str], record_tape=None, rediscover_s: float = 60.0,
                        private_channels: bool = False, max_markets: int = 50) -> Dict[str, Any]:
    from alphalab.kalshi.discovery import discover, rank_for_recording
    from alphalab.kalshi.ws import PRIVATE_CHANNELS, PUBLIC_CHANNELS, KalshiWebSocket

    def pick() -> List[str]:
        chosen = list(markets)
        if series:
            found = rank_for_recording(discover(rest, series), min_seconds_to_close=30)[:max_markets]
            for mm in found:
                row = mm.to_row()
                session.register_market(mm.ticker, row)
                if record_tape:
                    record_tape.write("market", {**mm.extra, "ticker": mm.ticker, "event_ticker": mm.event_ticker,
                                                 "close_time": mm.close_ts, "status": mm.status,
                                                 "floor_strike": mm.floor_strike, "strike_type": mm.strike_type})
            chosen += [m.ticker for m in found]
        return list(dict.fromkeys(chosen))

    initial = await asyncio.to_thread(pick)
    channels = list(PUBLIC_CHANNELS) + (list(PRIVATE_CHANNELS) if private_channels else [])

    def on_status(status: str, info: Dict[str, Any]) -> None:
        if status == "connected":
            session.on_connection(True)
        elif status in ("disconnected", "reconnecting"):
            session.on_connection(False)
        if record_tape:
            record_tape.write("note", {"status": status, **info})

    def on_event(ev):
        if isinstance(ev, dict):  # private channel frames
            handler = getattr(session.broker, "on_private", None)
            if handler:
                handler(ev)
            return
        session.on_event(ev)

    ws = KalshiWebSocket(ws_url, signer, initial, channels=channels, on_event=on_event,
                         on_raw=(lambda raw, ns: record_tape.write("ws", raw, ns)) if record_tape else None,
                         on_status=on_status)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    async def ticker():
        while not stop.is_set():
            session.tick(time.time_ns())
            if session.kill.is_tripped() and session.mode == "live":
                pass  # session.tick already cancelled everything; keep feed alive for monitoring
            await asyncio.sleep(0.1)

    async def rediscover():
        while not stop.is_set():
            await asyncio.sleep(rediscover_s)
            try:
                new = await asyncio.to_thread(pick)
                await ws.add_markets(new)
            except Exception as exc:
                log.warning("rediscover_failed", extra={"fields": {"error": str(exc)[:200]}})

    async def settlements():
        """Poll results of closed markets we hold, in case lifecycle events are missed."""
        while not stop.is_set():
            await asyncio.sleep(30)
            held = {m for (_, m), p in session.portfolio.open_positions().items()}
            now = time.time()
            for m in held:
                close = (session.meta.get(m) or {}).get("close_ts")
                if close and now > close:
                    try:
                        info = await asyncio.to_thread(rest.get_market, m)
                    except Exception:
                        continue
                    res = (info.get("result") or "").lower()
                    if res in ("yes", "no"):
                        session.on_event(Settlement(time.time_ns(), m, 1.0 if res == "yes" else 0.0, res))

    tasks = [asyncio.create_task(ws.run()), asyncio.create_task(ticker()), asyncio.create_task(rediscover()),
             asyncio.create_task(settlements())]
    await stop.wait()
    await ws.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if record_tape:
        record_tape.close()
    shutdown = getattr(session.broker, "shutdown", None)
    if shutdown:
        await asyncio.to_thread(shutdown)
    return session.stop("signal")
