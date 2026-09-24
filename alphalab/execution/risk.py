"""Portfolio risk engine - the same checks run in backtest, paper and live.

Pre-trade checks (``check_order``), in order:
  kill switch / halted -> disconnected -> stale data -> settlement protection
  -> price sanity -> order size -> duplicate / client id reuse -> order rate
  -> open-order count -> per-market position -> per-market, per-strategy and
  total exposure (worst case, counting resting orders as if filled).

Post-trade monitoring (``on_equity``): daily loss limit and max drawdown halt
trading and (outside backtests) trip the latched kill switch.

Orders that strictly reduce an existing position skip the position/exposure
limits (so the system can always de-risk) but never skip the kill switch,
stale-data, connectivity or price-sanity checks.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from alphalab.core.config import RiskConfig
from alphalab.core.prices import PRICE_SCALE
from alphalab.execution.killswitch import KillSwitch

log = logging.getLogger(__name__)
NS = 1_000_000_000


@dataclass
class RiskDecision:
    ok: bool
    reason: str = ""


class RiskEngine:
    def __init__(self, cfg: RiskConfig, mode: str = "backtest", kill_switch: Optional[KillSwitch] = None,
                 on_event: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
                 market_close_ns: Optional[Dict[str, int]] = None):
        self.cfg = cfg
        self.mode = mode
        self.kill = kill_switch
        self.on_event = on_event
        self.market_close_ns = dict(market_close_ns or {})
        self.halted = False
        self.halt_reason = ""
        self.connected = True
        self.last_data_ns: Dict[str, int] = {}
        self._order_times: Deque[int] = deque()
        self._fingerprints: Dict[Tuple, int] = {}
        self._client_ids: set = set()
        self._day: Optional[str] = None
        self._day_start_equity = 0.0
        self._peak_equity = 0.0
        self.daily_pnl = 0.0
        self.drawdown = 0.0
        self.rejections: Dict[str, int] = {}

    # ------------------------------------------------------------------ events
    def _event(self, typ: str, severity: str, **detail: Any) -> None:
        log.log(logging.WARNING if severity != "info" else logging.INFO, "risk_event",
                extra={"fields": {"type": typ, "severity": severity, "mode": self.mode, **detail}})
        if self.on_event:
            self.on_event(typ, severity, detail)

    def halt(self, reason: str, now_ns: Optional[int] = None) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            self._event("halt", "critical", reason=reason, ts_ns=now_ns)
            if self.kill is not None and self.mode in ("paper", "live"):
                self.kill.trip(reason, source="risk_engine")

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""

    def on_data(self, market: str, now_ns: int) -> None:
        self.last_data_ns[market] = now_ns

    def on_connection(self, connected: bool, now_ns: Optional[int] = None) -> None:
        if connected != self.connected:
            self._event("connection", "warning" if not connected else "info", connected=connected, ts_ns=now_ns)
        self.connected = connected

    def stale_markets(self, now_ns: int, markets: Iterable[str]) -> List[str]:
        lim = int(self.cfg.stale_data_s * NS)
        return [m for m in markets if now_ns - self.last_data_ns.get(m, 0) > lim]

    def is_kill_tripped(self) -> bool:
        return bool(self.kill is not None and self.kill.is_tripped())

    def on_equity(self, now_ns: int, equity: float) -> None:
        day = datetime.fromtimestamp(now_ns / NS, timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
        self._peak_equity = max(self._peak_equity, equity)
        self.daily_pnl = equity - self._day_start_equity
        self.drawdown = self._peak_equity - equity
        if self.daily_pnl <= -abs(self.cfg.max_daily_loss_usd):
            self.halt(f"daily_loss_limit ({self.daily_pnl:.2f} <= -{self.cfg.max_daily_loss_usd})", now_ns)
        elif self.drawdown >= abs(self.cfg.max_drawdown_usd):
            self.halt(f"max_drawdown ({self.drawdown:.2f} >= {self.cfg.max_drawdown_usd})", now_ns)

    # ------------------------------------------------------------------ checks
    def _reject(self, reason: str, **detail: Any) -> RiskDecision:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        self._event("order_rejected", "info" if self.mode == "backtest" else "warning", reason=reason, **detail)
        return RiskDecision(False, reason)

    def check_order(self, *, strategy: str, market: str, action: str, price: int, qty: float,
                    now_ns: int, mid: Optional[float], positions: Dict[Tuple[str, str], Tuple[float, float]],
                    open_orders: Iterable[Any], client_order_id: Optional[str] = None) -> RiskDecision:
        """``positions``: {(strategy, market): (qty, avg_price_dollars)};
        ``open_orders``: objects with ``req`` (OrderRequest) and ``remaining``."""
        cfg = self.cfg
        detail = dict(strategy=strategy, market=market, action=action, price=price, qty=qty)
        if self.halted:
            return self._reject("halted:" + self.halt_reason, **detail)
        if self.is_kill_tripped():
            return self._reject("kill_switch", **detail)
        if not self.connected:
            return self._reject("disconnected", **detail)
        last = self.last_data_ns.get(market)
        if last is None or now_ns - last > cfg.stale_data_s * NS:
            return self._reject("stale_data", **detail)
        close = self.market_close_ns.get(market)
        if close is not None and close - now_ns < cfg.settlement_buffer_s * NS:
            return self._reject("settlement_protection", **detail)
        if not (cfg.min_price_units <= price <= cfg.max_price_units):
            return self._reject("price_out_of_bounds", **detail)
        if mid is None:
            return self._reject("no_two_sided_market", **detail)
        if abs(price - mid) > cfg.max_distance_from_mid_units:
            return self._reject("price_far_from_mid", **detail)
        if qty <= 0 or qty > cfg.max_order_size:
            return self._reject("order_size", **detail)
        if client_order_id:
            if client_order_id in self._client_ids:
                return self._reject("duplicate_client_order_id", **detail)
        fp = (strategy, market, action, price, qty)
        win = int(cfg.duplicate_window_s * NS)
        prev = self._fingerprints.get(fp)
        if prev is not None and now_ns - prev < win:
            return self._reject("duplicate_order", **detail)
        open_orders = list(open_orders)
        if any(o.req.market == market and o.req.action == action and o.req.price == price
               and o.req.strategy == strategy and o.remaining > 0 for o in open_orders):
            return self._reject("duplicate_resting_order", **detail)
        while self._order_times and now_ns - self._order_times[0] > NS:
            self._order_times.popleft()
        if len(self._order_times) >= cfg.max_orders_per_second:
            return self._reject("order_rate", **detail)
        if len(open_orders) >= cfg.max_open_orders:
            return self._reject("max_open_orders", **detail)

        signed = qty if action == "buy" else -qty
        mkt_pos = sum(q for (s, m), (q, _) in positions.items() if m == market)
        reduces = mkt_pos != 0 and (mkt_pos > 0) != (signed > 0) and qty <= abs(mkt_pos)
        if not reduces:
            pending = sum((o.remaining if o.req.action == action else 0.0) for o in open_orders
                          if o.req.market == market)
            worst = abs(mkt_pos + (signed + (pending if action == "buy" else -pending)))
            if worst > cfg.max_position_per_market:
                return self._reject("max_position_per_market", worst=worst, **detail)
            new_risk = qty * (price / PRICE_SCALE if action == "buy" else 1 - price / PRICE_SCALE)
            open_risk = self._open_order_risk(open_orders)
            mkt = self._cap(positions, market=market) + open_risk.get(("m", market), 0.0) + new_risk
            if mkt > cfg.max_market_exposure_usd:
                return self._reject("max_market_exposure", exposure=round(mkt, 2), **detail)
            strat = self._cap(positions, strategy=strategy) + open_risk.get(("s", strategy), 0.0) + new_risk
            if strat > cfg.max_strategy_exposure_usd:
                return self._reject("max_strategy_exposure", exposure=round(strat, 2), **detail)
            tot = self._cap(positions) + open_risk.get(("t", ""), 0.0) + new_risk
            if tot > cfg.max_total_exposure_usd:
                return self._reject("max_total_exposure", exposure=round(tot, 2), **detail)
        # accepted: record for rate / duplicate tracking
        self._order_times.append(now_ns)
        self._fingerprints[fp] = now_ns
        if len(self._fingerprints) > 5000:
            cutoff = now_ns - win
            self._fingerprints = {k: v for k, v in self._fingerprints.items() if v >= cutoff}
        if client_order_id:
            self._client_ids.add(client_order_id)
        return RiskDecision(True)

    @staticmethod
    def _cap(positions: Dict[Tuple[str, str], Tuple[float, float]], market: Optional[str] = None,
             strategy: Optional[str] = None) -> float:
        tot = 0.0
        for (s, m), (q, avg) in positions.items():
            if (market and m != market) or (strategy and s != strategy) or q == 0:
                continue
            tot += q * avg if q > 0 else -q * (1 - avg)
        return tot

    @staticmethod
    def _open_order_risk(open_orders: List[Any]) -> Dict[Tuple[str, str], float]:
        out: Dict[Tuple[str, str], float] = {}
        for o in open_orders:
            p = o.req.price / PRICE_SCALE
            r = o.remaining * (p if o.req.action == "buy" else 1 - p)
            for k in (("m", o.req.market), ("s", o.req.strategy), ("t", "")):
                out[k] = out.get(k, 0.0) + r
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {"halted": self.halted, "halt_reason": self.halt_reason, "connected": self.connected,
                "kill_switch": self.is_kill_tripped(), "daily_pnl": round(self.daily_pnl, 4),
                "drawdown": round(self.drawdown, 4), "rejections": dict(self.rejections),
                "limits": self.cfg.__dict__}
