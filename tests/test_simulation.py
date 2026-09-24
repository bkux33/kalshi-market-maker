import pytest

from alphalab.core.config import FillConfig, RiskConfig
from alphalab.core.events import BookDelta, BookSnapshot, Settlement, TradeEvent
from alphalab.data.synthetic import SyntheticSpec, write_synthetic
from alphalab.sim.backtest import BacktestConfig, run_backtest
from alphalab.sim.broker import OrderRequest
from alphalab.sim.portfolio import Portfolio
from alphalab.sim.replay import ReplaySource, ReplaySpec
from alphalab.strategies import get_strategy
from alphalab.strategies.base import Strategy
from tests.conftest import C, make_broker, snap

NS = 1_000_000_000


# ---------------------------------------------------------------- portfolio / P&L
def test_long_round_trip_pnl_and_fees():
    pf = Portfolio()
    pf.on_fill("s", "M", "buy", 40 * C, 10, 0.17, 1)
    pf.on_fill("s", "M", "sell", 45 * C, 10, 0.18, 2)
    t = pf.trips[0]
    assert t.gross == pytest.approx(0.50) and t.fees == pytest.approx(0.35) and t.net == pytest.approx(0.15)
    assert pf.cash == pytest.approx(0.15) and pf.pos("s", "M").qty == 0


def test_short_flip_and_settlement():
    pf = Portfolio()
    pf.on_fill("s", "M", "sell", 60 * C, 5, 0.0, 1)       # short 5 YES @60 (== long 5 NO @40)
    assert pf.capital_at_risk() == pytest.approx(5 * 0.40)
    pf.on_fill("s", "M", "buy", 50 * C, 8, 0.0, 2)        # cover 5 (+0.50) and go long 3 @50
    assert pf.trips[0].gross == pytest.approx(0.50) and pf.pos("s", "M").qty == 3
    pf.settle("M", 0.0, 3)                                 # NO wins: long 3 YES @50 loses 1.50
    assert pf.trips[1].gross == pytest.approx(-1.50) and pf.trips[1].exit_reason == "settlement"
    assert pf.cash == pytest.approx(0.50 - 1.50)


def test_flip_fee_split():
    pf = Portfolio()
    pf.on_fill("s", "M", "buy", 50 * C, 2, 0.02, 1)
    pf.on_fill("s", "M", "sell", 50 * C, 4, 0.04, 2)      # closes 2, opens short 2: fee split 50/50
    assert pf.trips[0].fees == pytest.approx(0.04)
    assert pf.pos("s", "M").open_trip.fees == pytest.approx(0.02)


# ---------------------------------------------------------------- taker fills
def test_taker_partial_fill_walks_book_and_ioc_cancels_rest():
    b, books, pf = make_broker()
    snap(books, b, 1, yes=[(45 * C, 10)], no=[(50 * C, 3), (49 * C, 4)])
    o = b.submit(OrderRequest("s", "M", "buy", 51 * C, 10, tif="ioc"), 1)
    b.process_timers(1)
    assert [(f.price, f.qty) for f in b.fills] == [(50 * C, 3), (51 * C, 4)]
    assert o.status == "canceled" and o.filled == 7 and o.reason == "ioc_remainder"
    assert all(f.liquidity == "taker" for f in b.fills) and b.fills[0].fee > 0
    # the same displayed liquidity cannot be taken twice (impact overlay)
    o2 = b.submit(OrderRequest("s", "M", "buy", 51 * C, 5, tif="ioc"), 2)
    b.process_timers(2)
    assert o2.filled == 0


def test_latency_uses_book_at_activation():
    b, books, pf = make_broker(FillConfig(order_latency_ms=200, cancel_latency_ms=0))
    snap(books, b, 0, no=[(50 * C, 10)])                    # ask 50
    b.submit(OrderRequest("s", "M", "buy", 50 * C, 5, tif="ioc"), 0)
    snap(books, b, 100 * 1_000_000, no=[(47 * C, 10)])      # ask moves to 53 before we arrive
    b.process_timers(200 * 1_000_000)
    assert b.fills == []                                    # limit 50 no longer marketable -> IOC dies


def test_post_only_rejected_when_crossing_and_self_cross():
    b, books, pf = make_broker()
    snap(books, b, 1)
    o = b.submit(OrderRequest("s", "M", "buy", 50 * C, 1, post_only=True), 1)
    b.process_timers(1)
    assert o.status == "rejected" and o.reason == "post_only_would_cross"
    a = b.submit(OrderRequest("s", "M", "sell", 48 * C, 1, post_only=True), 1)
    c = b.submit(OrderRequest("s", "M", "buy", 48 * C, 1), 1)
    b.process_timers(1)
    assert a.status == "resting" and c.status == "rejected" and c.reason == "self_cross"


# ---------------------------------------------------------------- maker fills / queue model
def test_queue_position_trade_consumes_queue_first():
    b, books, pf = make_broker()
    snap(books, b, 1, yes=[(45 * C, 10)], no=[(50 * C, 10)])
    o = b.submit(OrderRequest("s", "M", "buy", 45 * C, 5), 1)
    b.process_timers(1)
    assert o.status == "resting" and o.queue_ahead == 10
    b.on_trade(TradeEvent(2, "M", 45 * C, 8, "no"))        # sells hit bids: 8 of the 10 ahead
    assert o.filled == 0 and o.queue_ahead == pytest.approx(2)
    b.on_trade(TradeEvent(3, "M", 45 * C, 4, "no"))        # 2 more ahead, then 2 fill us
    assert o.filled == 2 and b.fills[-1].liquidity == "maker" and b.fills[-1].price == 45 * C


def test_trade_through_fills_and_cancel_models():
    b, books, pf = make_broker()
    snap(books, b, 1, yes=[(45 * C, 10)], no=[(50 * C, 10)])
    o = b.submit(OrderRequest("s", "M", "buy", 46 * C, 5), 1)  # improves the bid: nobody ahead
    b.process_timers(1)
    assert o.queue_ahead == 0
    b.on_trade(TradeEvent(2, "M", 45 * C, 3, "no"))        # print below our bid -> we'd have been hit first
    assert o.filled == 3
    # pro-rata vs back cancellation attribution
    for model, expected in (("pro_rata", 5.0), ("back", 10.0), ("front", 0.0)):
        b2, books2, _ = make_broker(FillConfig(order_latency_ms=0, queue_cancel_model=model))
        snap(books2, b2, 1, yes=[(45 * C, 20)], no=[(50 * C, 10)])
        o2 = b2.submit(OrderRequest("s", "M", "buy", 45 * C, 5), 1)
        b2.process_timers(1)
        o2.queue_ahead = 10.0  # 10 ahead of us out of 20 displayed
        ev = BookDelta(2, "M", "yes", 45 * C, -10)
        b2.before_book(ev)
        books2.apply(ev)
        b2.after_book(ev)
        assert o2.queue_ahead == pytest.approx(expected), model


def test_cross_fill_is_adverse_and_capped_by_visible_size():
    b, books, pf = make_broker()
    snap(books, b, 1, yes=[(45 * C, 10)], no=[(50 * C, 10)])
    o = b.submit(OrderRequest("s", "M", "buy", 46 * C, 20), 1)
    b.process_timers(1)
    snap(books, b, 2, yes=[(44 * C, 10)], no=[(55 * C, 7)])  # ask drops to 45 < our 46 bid
    assert o.filled == 7 and b.fills[0].price == 46 * C and b.fills[0].liquidity == "maker"
    snap(books, b, 3, yes=[(44 * C, 10)], no=[(55 * C, 7)])  # same liquidity is not re-used
    assert o.filled == 7


def test_cancel_latency_allows_fill_before_cancel_takes_effect():
    b, books, pf = make_broker(FillConfig(order_latency_ms=0, cancel_latency_ms=500))
    snap(books, b, 0, yes=[(45 * C, 0.0001)], no=[(50 * C, 10)])
    o = b.submit(OrderRequest("s", "M", "buy", 46 * C, 5), 0)
    b.process_timers(0)
    b.cancel(o.order_id, 100)
    b.process_timers(200 * 1_000_000)
    b.on_trade(TradeEvent(300 * 1_000_000, "M", 46 * C, 2, "no"))
    assert o.filled == 2
    b.process_timers(700 * 1_000_000)
    assert o.status == "canceled"


def test_no_fills_after_close_or_settlement():
    b, books, pf = make_broker(close={"M": 10 * NS})
    snap(books, b, 1)
    o = b.submit(OrderRequest("s", "M", "buy", 44 * C, 5), 1)
    b.process_timers(1)
    b.close_market("M", 10 * NS)
    assert o.status == "canceled"
    late = b.submit(OrderRequest("s", "M", "buy", 44 * C, 5), 11 * NS)
    assert late.status == "rejected" and late.reason == "market_closed"
    b.on_settlement(Settlement(12 * NS, "M", 1.0))
    assert "M" in b.closed_markets


def test_cross_only_mode_ignores_trades():
    b, books, pf = make_broker(FillConfig(order_latency_ms=0, maker_fill_mode="cross_only"))
    snap(books, b, 1, yes=[(45 * C, 1)], no=[(50 * C, 10)])
    o = b.submit(OrderRequest("s", "M", "buy", 45 * C, 5), 1)
    b.process_timers(1)
    b.on_trade(TradeEvent(2, "M", 44 * C, 10, "no"))
    assert o.filled == 0


# ---------------------------------------------------------------- backtest engine
class Recorder(Strategy):
    name = "recorder"

    @classmethod
    def default_params(cls):
        return {}

    def on_start(self, ctx):
        self.state["seen"] = []

    def on_book(self, ctx, market):
        f = ctx.features(market)
        self.state["seen"].append((ctx.now_ns, f["ts_ns"]))


def test_backtest_determinism_and_no_lookahead(db):
    write_synthetic(db, SyntheticSpec(n_markets=3, seed=4, imbalance_signal=0.5))
    cls = get_strategy("imbalance")
    cfg = BacktestConfig()
    r1 = run_backtest(db, lambda: cls(threshold=0.6), cfg, persist=False)
    r2 = run_backtest(db, lambda: cls(threshold=0.6), cfg, persist=False)
    assert r1.result_hash == r2.result_hash and r1.metrics["n_fills"] > 0
    assert r1.metrics["net_pnl"] == pytest.approx(r1.metrics["gross_pnl"] - r1.metrics["fees"])
    assert r1.metrics["gross_pnl"] == pytest.approx(r1.metrics["mid_pnl"] - r1.metrics["exec_cost_vs_mid"])
    rec = Recorder()
    run_backtest(db, lambda: rec, cfg, persist=False)
    assert all(feat_ts <= now for now, feat_ts in rec.state["seen"])
    # fills never occur before the order is active (latency respected)
    orders = r1.orders.set_index("order_id")
    for f in r1.fills.itertuples():
        assert f.ts_ns >= orders.loc[f.order_id, "ts_active"]


def test_backtest_persists_and_settles(db):
    write_synthetic(db, SyntheticSpec(n_markets=2, seed=9))
    cls = get_strategy("crypto15m_fair_value")
    r = run_backtest(db, lambda: cls(series_prefix="KXSYN", edge_c=1.0), BacktestConfig())
    assert db.query_df("SELECT COUNT(*) n FROM runs")["n"].iloc[0] == 1
    if r.metrics["n_trades"]:
        assert set(r.trips["exit_reason"]) <= {"settlement", "flat"}
        assert r.metrics["open_positions_at_end"] == 0
    assert db.integrity_report()["orphan_fills"] == 0


def test_time_split_warmup_rebuilds_books(db):
    write_synthetic(db, SyntheticSpec(n_markets=1, seed=3))
    lo, hi = db.query_df("SELECT MIN(ts_ns) lo, MAX(ts_ns) hi FROM book_events").iloc[0]
    mid = int((lo + hi) // 2)
    src = ReplaySource(db, ReplaySpec(start_ns=mid))
    assert src.read_from_ns <= mid
    rec = Recorder()
    run_backtest(db, lambda: rec, BacktestConfig(replay=ReplaySpec(start_ns=mid)), persist=False)
    assert rec.state["seen"] and min(t for t, _ in rec.state["seen"]) >= mid
