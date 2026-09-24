import dataclasses
import json
import time

import pytest

from alphalab.core.config import LIVE_ACK_VALUE, RiskConfig, load_settings
from alphalab.execution.killswitch import KillSwitch
from alphalab.execution.live import LiveBroker, LiveTradingRefused, assert_live_allowed, order_body
from alphalab.execution.risk import RiskEngine
from alphalab.sim.broker import OrderRequest, SimOrder
from tests.conftest import C

NS = 1_000_000_000
MID = 50 * C


def engine(**kw):
    cfg = RiskConfig(**{"max_order_size": 10, "max_position_per_market": 20, "max_market_exposure_usd": 8.0,
                        "max_total_exposure_usd": 12.0, "max_strategy_exposure_usd": 10.0,
                        "max_daily_loss_usd": 5.0, "max_drawdown_usd": 8.0, "max_orders_per_second": 3,
                        "max_open_orders": 4, "stale_data_s": 5.0, **kw})
    r = RiskEngine(cfg, mode="backtest", market_close_ns={"M": 1000 * NS})
    r.on_data("M", 100 * NS)
    r.on_data("N", 100 * NS)
    return r


def check(r, now=100 * NS, market="M", action="buy", price=MID, qty=5, positions=None, open_orders=(), mid=MID,
          strategy="s", coid=None):
    return r.check_order(strategy=strategy, market=market, action=action, price=price, qty=qty, now_ns=now, mid=mid,
                         positions=positions or {}, open_orders=list(open_orders), client_order_id=coid)


def resting(market, action, price, qty, strategy="s"):
    return SimOrder("x", OrderRequest(strategy, market, action, price, qty), 0, 0, status="resting", remaining=qty)


def test_basic_limits():
    r = engine()
    assert check(r).ok
    assert check(r, qty=11).reason == "order_size"
    assert check(r, price=50, mid=50).reason == "price_out_of_bounds"
    assert check(r, price=80 * C).reason == "price_far_from_mid"
    assert check(r, mid=None).reason == "no_two_sided_market"
    assert check(r, now=106 * NS).reason == "stale_data"
    assert check(r, now=990 * NS).reason in ("stale_data", "settlement_protection")
    r.on_data("M", 990 * NS)
    assert check(r, now=990 * NS).reason == "settlement_protection"


def test_position_and_exposure_limits_and_reduce_only_bypass():
    r = engine()
    pos = {("s", "M"): (18.0, 0.5)}
    assert check(r, qty=5, positions=pos).reason == "max_position_per_market"
    # reducing a position is always allowed even when limits are exceeded
    assert check(r, action="sell", qty=5, positions={("s", "M"): (40.0, 0.5)}).ok
    r2 = engine()
    assert check(r2, qty=10, positions={("s", "M"): (10.0, 0.5)}).reason == "max_market_exposure"   # 5 + 5 > 8
    r3 = engine()
    assert check(r3, market="N", qty=7, positions={("s", "M"): (14.0, 0.5)}).reason == "max_strategy_exposure"
    r4 = engine(max_strategy_exposure_usd=100)
    assert check(r4, market="N", qty=6, positions={("s2", "M"): (20.0, 0.5)}).reason == "max_total_exposure"
    # resting orders count as if filled (worst case)
    r5 = engine()
    assert check(r5, qty=5, open_orders=[resting("M", "buy", 60 * C, 10)]).reason == "max_market_exposure"


def test_duplicates_rate_and_open_orders():
    r = engine()
    assert check(r, coid="a").ok
    assert check(r, coid="a", price=49 * C).reason == "duplicate_client_order_id"
    assert check(r).reason == "duplicate_order"                       # same fingerprint within 2s
    assert check(r, now=103 * NS).ok                                  # outside the window
    r2 = engine()
    assert check(r2, price=49 * C, open_orders=[resting("M", "buy", 49 * C, 1)]).reason == "duplicate_resting_order"
    r3 = engine(max_orders_per_second=2)
    assert check(r3, price=48 * C).ok and check(r3, price=47 * C).ok
    assert check(r3, price=46 * C).reason == "order_rate"
    r4 = engine()
    many = [resting("N", "buy", (40 + i) * C, 0.01) for i in range(4)]
    assert check(r4, open_orders=many).reason == "max_open_orders"


def test_daily_loss_and_drawdown_halt_trip_kill_switch(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    r = RiskEngine(RiskConfig(max_daily_loss_usd=5, max_drawdown_usd=100), mode="paper", kill_switch=ks)
    r.on_data("M", 1)
    t0 = int(time.time()) * NS
    r.on_equity(t0, 0.0)
    r.on_equity(t0 + NS, -4.0)
    assert not r.halted
    r.on_equity(t0 + 2 * NS, -5.5)
    assert r.halted and ks.is_tripped(force=True) and "daily_loss" in r.halt_reason
    assert check(r, now=t0 + 3 * NS).ok is False
    r2 = RiskEngine(RiskConfig(max_daily_loss_usd=100, max_drawdown_usd=3), mode="backtest")
    r2.on_equity(t0, 10.0)
    r2.on_equity(t0 + NS, 6.9)
    assert r2.halted and "drawdown" in r2.halt_reason


def test_kill_switch_and_disconnect_block_orders(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    r = RiskEngine(RiskConfig(), mode="paper", kill_switch=ks)
    r.on_data("M", 100 * NS)
    assert check(r).ok
    ks.trip("test")
    ks._last_check = 0
    assert check(r, price=49 * C).reason == "kill_switch"
    assert ks.reset() and not ks.is_tripped(force=True)
    r.on_connection(False)
    assert check(r, price=48 * C).reason == "disconnected"


# ---------------------------------------------------------------- live-mode protection
def live_settings(tmp_path, **env):
    key = tmp_path / "k.pem"
    key.write_text("x")
    base = {"TRADING_MODE": "live", "LIVE_TRADING_ACK": LIVE_ACK_VALUE, "KALSHI_API_KEY_ID": "id",
            "KALSHI_PRIVATE_KEY_PATH": str(key), "DATA_DIR": str(tmp_path / "data")}
    base.update(env)
    return load_settings(env={k: v for k, v in base.items() if v is not None})


def test_live_refused_by_default_and_for_each_missing_gate(tmp_path, db):
    with pytest.raises(LiveTradingRefused, match="TRADING_MODE"):
        assert_live_allowed(load_settings(env={"DATA_DIR": str(tmp_path)}), db, ["x"])
    with pytest.raises(LiveTradingRefused, match="LIVE_TRADING_ACK"):
        assert_live_allowed(live_settings(tmp_path, LIVE_TRADING_ACK="yes"), db, ["x"])
    with pytest.raises(LiveTradingRefused, match="KALSHI_API_KEY_ID"):
        assert_live_allowed(live_settings(tmp_path, KALSHI_API_KEY_ID=None), db, ["x"])
    with pytest.raises(LiveTradingRefused, match="UNVALIDATED"):
        assert_live_allowed(live_settings(tmp_path), db, ["imbalance:{}"])
    db.insert_rows("strategy_status", [dict(strategy_key="imbalance:{}", strategy="imbalance", params="{}",
                                            status="PAPER", experiment_id="e", updated_ns=1, reason="")])
    with pytest.raises(LiveTradingRefused, match="PAPER"):
        assert_live_allowed(live_settings(tmp_path), db, ["imbalance:{}"])
    db.execute("UPDATE strategy_status SET status = 'LIVE-CANDIDATE'")
    s = live_settings(tmp_path)
    assert_live_allowed(s, db, ["imbalance:{}"])  # all gates pass
    KillSwitch(s.risk.kill_switch_file).trip("t")
    with pytest.raises(LiveTradingRefused, match="kill switch"):
        assert_live_allowed(s, db, ["imbalance:{}"])
    KillSwitch(s.risk.kill_switch_file).reset()
    s.risk.max_daily_loss_usd = float("inf")
    with pytest.raises(LiveTradingRefused, match="max_daily_loss_usd"):
        assert_live_allowed(s, db, ["imbalance:{}"])


def test_demo_integration_only_on_demo(tmp_path):
    assert_live_allowed(live_settings(tmp_path, KALSHI_ENV="demo"), None, ["x"], demo_integration=True)
    with pytest.raises(LiveTradingRefused, match="demo"):
        assert_live_allowed(live_settings(tmp_path, KALSHI_ENV="prod"), None, ["x"], demo_integration=True)


def test_order_body_mapping():
    b = order_body(OrderRequest("s", "M", "buy", 45 * C, 3, tif="gtc", post_only=True), "cid")
    assert (b["side"], b["action"], b["yes_price_dollars"], b["count"], b["post_only"]) == ("yes", "buy", "0.4500", 3, True)
    b = order_body(OrderRequest("s", "M", "sell", 45 * C, 3, tif="ioc"), "cid")
    assert (b["side"], b["action"], b["no_price_dollars"], b["time_in_force"]) == ("no", "buy", "0.5500", "immediate_or_cancel")
    assert "yes_price_dollars" not in b


class FakeRest:
    def __init__(self):
        self.created, self.cancelled = [], []

    def create_order(self, body):
        self.created.append(body)
        return {"order_id": f"EX{len(self.created)}", "status": "resting"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {}


def test_live_broker_fills_dedupe_and_cancel():
    from alphalab.core.book_manager import BookManager
    from alphalab.core.fees import FeeModel
    from alphalab.sim.portfolio import Portfolio
    rest = FakeRest()
    pf = Portfolio()
    b = LiveBroker(rest, BookManager(), FeeModel(), pf, {})
    fills = []
    b.on_fill = fills.append
    o = b.submit(OrderRequest("s", "M", "buy", 45 * C, 4), 1)
    assert rest.created[0]["client_order_id"] and o.status == "resting"
    msg = {"trade_id": "t1", "order_id": "EX1", "count_fp": "3.00", "yes_price_dollars": "0.4500", "is_taker": False}
    b.on_private({"private": "fill", "msg": msg})
    b.on_private({"private": "fill", "msg": msg})  # duplicate delivery ignored
    assert len(fills) == 1 and pf.pos("s", "M").qty == 3 and o.remaining == 1
    b.cancel(o.order_id, 2)
    assert rest.cancelled == ["EX1"] and o.status == "canceled"
