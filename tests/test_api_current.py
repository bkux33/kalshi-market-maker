"""Regression tests against the CURRENT Kalshi API formats.

Message shapes follow Kalshi's AsyncAPI/OpenAPI specifications and the official SDK
(kalshi-python-sync 3.30.0, 2026-09-15): fixed-point dollar strings (``*_dollars``),
fixed-point counts (``*_fp``), ``ts_ms`` matching-engine timestamps, V2 orders.
"""

import json

import httpx
import pytest

from alphalab.core.book_manager import BookManager
from alphalab.core.config import (DEMO_REST, DEMO_WS, LEGACY_DEMO_REST, PROD_REST, is_demo_url, load_settings)
from alphalab.core.fees import FeeModel
from alphalab.kalshi.discovery import market_from_api, rank_for_recording
from alphalab.kalshi.messages import _ts_to_ns, exch_ts_ns, iso_to_ns, parse_frame
from alphalab.kalshi.rest import KalshiREST, parse_orderbook_response

RECV = 1_790_000_000_123_456_789  # receive time in ns


def snap(sid, seq, market="KXBTC15M-26SEP241500-00", yes=(("0.4200", "100.00"), ("0.4100", "50.00")),
         no=(("0.5500", "80.00"),), with_levels=True):
    msg = {"market_ticker": market, "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1"}
    if with_levels:
        msg["yes_dollars_fp"] = [list(x) for x in yes]
        msg["no_dollars_fp"] = [list(x) for x in no]
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg}


def delta(sid, seq, price, d, side="yes", market="KXBTC15M-26SEP241500-00", ts_ms=1790000000123):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": market, "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
                    "price_dollars": price, "delta_fp": d, "side": side, "ts_ms": ts_ms}}


def feed(bm, frames):
    out = []
    for i, f in enumerate(frames):
        for ev in parse_frame(f, RECV + i):
            out.append(bm.apply(ev))
    return out


# ---------------------------------------------------------------- order-book engine
def test_snapshot_updates_size_price_and_removal():
    bm = BookManager()
    feed(bm, [snap(1, 1)])
    ob = bm.book("KXBTC15M-26SEP241500-00")
    assert ob.best_bid() == (4200, 100.0) and ob.best_ask() == (4500, 80.0)   # NO 0.55 -> YES ask 0.45
    feed(bm, [delta(1, 2, "0.4200", "-30.00")])                              # size change
    assert ob.best_bid() == (4200, 70.0)
    feed(bm, [delta(1, 3, "0.4300", "25.00")])                               # new better price
    assert ob.best_bid() == (4300, 25.0)
    feed(bm, [delta(1, 4, "0.4300", "-25.00")])                              # removal
    assert ob.best_bid() == (4200, 70.0) and 4300 not in ob.yes
    feed(bm, [delta(1, 5, "0.5600", "10.00", side="no")])                    # ask improves to 0.44
    assert ob.best_ask() == (4400, 10.0) and ob.spread() == 200
    assert bm.stats()["deltas_applied"] == 4 and bm.gaps == 0


def test_duplicates_are_ignored_not_double_applied():
    bm = BookManager()
    d = delta(1, 2, "0.4200", "5.00")
    feed(bm, [snap(1, 1), d, d, d])
    assert bm.book("KXBTC15M-26SEP241500-00").best_bid() == (4200, 105.0)
    assert bm.duplicates == 2 and bm.gaps == 0 and bm.is_synced("KXBTC15M-26SEP241500-00")


def test_gap_unsyncs_all_markets_on_sid_until_snapshot():
    bm = BookManager()
    feed(bm, [snap(1, 1, market="A"), snap(1, 2, market="B"), delta(1, 3, "0.4200", "1.00", market="A")])
    feed(bm, [delta(1, 5, "0.4200", "1.00", market="A")])   # seq 4 missing
    assert bm.gaps == 1 and not bm.is_synced("A") and not bm.is_synced("B")
    assert feed(bm, [delta(1, 6, "0.4200", "1.00", market="B")]) == [False]   # ignored while unsynced
    feed(bm, [snap(2, 1, market="A"), snap(2, 2, market="B")])                 # resubscription: new sid
    assert bm.is_synced("A") and bm.is_synced("B")
    assert bm.book("A").best_bid() == (4200, 100.0)


def test_reconnect_restarts_sequence_numbers():
    bm = BookManager()
    feed(bm, [snap(1, 1), delta(1, 2, "0.4200", "1.00"), delta(1, 3, "0.4200", "1.00")])
    bm.reset_all()                                           # WS client does this on disconnect
    feed(bm, [snap(1, 1), delta(1, 2, "0.4200", "-10.00")])  # same sid, seq restarts at 1
    assert bm.book("KXBTC15M-26SEP241500-00").best_bid() == (4200, 90.0) and bm.duplicates == 0
    # even without reset_all (e.g. replay of a recording spanning a reconnect) a lower-seq snapshot
    # starts a new stream instead of being dropped as a duplicate
    bm2 = BookManager()
    feed(bm2, [snap(1, 1), delta(1, 2, "0.4200", "1.00"), snap(1, 1), delta(1, 2, "0.4200", "-10.00")])
    assert bm2.book("KXBTC15M-26SEP241500-00").best_bid() == (4200, 90.0) and bm2.duplicates == 0


def test_closed_market_empty_snapshot_is_valid_empty_book():
    # Shape Kalshi sends for closed markets: no level arrays, empty market_id.
    f = {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {"market_ticker": "KXMLB-X", "market_id": ""}}
    bm = BookManager()
    feed(bm, [f])
    ob = bm.book("KXMLB-X")
    assert bm.is_synced("KXMLB-X") and ob.is_empty() and ob.mid() is None


def test_delta_and_trade_carry_matching_engine_time():
    d = parse_frame(delta(1, 2, "0.960", "-54.00"), RECV)[0]
    assert d.exch_ts_ns == 1790000000123 * 1_000_000 and d.ts_ns == RECV and d.price == 9600 and d.delta == -54.0
    t = parse_frame({"type": "trade", "sid": 11, "msg": {
        "trade_id": "d91bc706", "market_ticker": "M", "yes_price_dollars": "0.360", "no_price_dollars": "0.640",
        "count_fp": "136.00", "taker_side": "no", "ts": 1669149841, "ts_ms": 1669149841123}}, RECV)[0]
    assert t.exch_ts_ns == 1669149841123 * 1_000_000 and (t.price, t.qty, t.taker_side) == (3600, 136.0, "no")
    legacy = parse_frame({"type": "trade", "msg": {"market_ticker": "M", "yes_price_dollars": "0.36",
                                                   "count_fp": "1", "taker_side": "yes", "ts": 1669149841}}, RECV)[0]
    assert legacy.exch_ts_ns == 1669149841 * 1_000_000_000


def test_ws_client_suppresses_duplicate_frames_and_trades():
    import asyncio
    from alphalab.kalshi.ws import KalshiWebSocket
    ws = KalshiWebSocket("ws://127.0.0.1:1", None, ["KXBTC15M-26SEP241500-00"])
    got = []
    ws.on_event = got.append
    tr = {"type": "trade", "sid": 2, "msg": {"trade_id": "t1", "market_ticker": "KXBTC15M-26SEP241500-00",
                                            "yes_price_dollars": "0.42", "count_fp": "1.00", "taker_side": "yes"}}
    frames = [snap(1, 1), delta(1, 2, "0.4200", "1.00"), delta(1, 2, "0.4200", "1.00"), tr, tr, "not json{"]
    for f in frames:
        asyncio.run(ws._handle(f if isinstance(f, str) else json.dumps(f), RECV))
    assert [e.kind for e in got] == ["snapshot", "delta", "trade"]
    assert ws.stats["duplicates"] == 2 and ws.stats["malformed"] == 1
    assert ws.stats["by_type"]["orderbook_delta"] == 2


# ---------------------------------------------------------------- timestamps
@pytest.mark.parametrize("value,expected", [
    (1669149841, 1669149841_000_000_000),               # epoch seconds
    (1669149841123, 1669149841_123_000_000),            # epoch milliseconds
    (1669149841.5, 1669149841_500_000_000),             # float seconds
    ("2022-11-22T20:44:01Z", 1669149841_000_000_000),
    ("2022-11-22T20:44:01.123456789Z", 1669149841_123_456_789),   # nanosecond precision kept
    ("2022-11-22T20:44:01.123456+00:00", 1669149841_123_456_000),
    ("2022-11-22T15:44:01.5-05:00", 1669149841_500_000_000),      # offset handled
    ("garbage", None), (None, None), ("", None),
])
def test_timestamp_conversion(value, expected):
    assert _ts_to_ns(value) == expected


def test_ts_ms_preferred_and_iso_is_exact():
    assert exch_ts_ns({"ts": "2022-11-22T20:44:01Z", "ts_ms": 1669149841999}) == 1669149841_999_000_000
    # a naive float conversion would lose the last digits; the integer path must not
    assert iso_to_ns("2026-09-24T21:15:00.000000001Z") % 1000 == 1


# ---------------------------------------------------------------- REST: current shapes
def test_rest_orderbook_fp_and_v2_order_paths():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.url.params), req.content))
        if req.url.path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.4200", "10.00"]],
                                                              "no_dollars": [["0.5500", "2.50"]]}})
        if req.method == "POST" and req.url.path.endswith("/portfolio/events/orders"):
            return httpx.Response(201, json={"order_id": "o1", "fill_count": "0.00", "remaining_count": "1.00",
                                             "ts_ms": 1})
        if req.method == "DELETE":
            return httpx.Response(200, json={"order_id": "o1", "reduced_by": "1.00", "ts_ms": 2})
        return httpx.Response(404)

    from alphalab.kalshi.auth import KalshiSigner
    from cryptography.hazmat.primitives.asymmetric import rsa
    signer = KalshiSigner("kid", rsa.generate_private_key(public_exponent=65537, key_size=2048))
    r = KalshiREST(DEMO_REST, signer, requests_per_second=1000, transport=httpx.MockTransport(handler),
                   sleep=lambda s: None)
    assert r.get_orderbook("M") == ([(4200, 10.0)], [(5500, 2.5)])
    assert r.create_order_v2({"ticker": "M"})["order_id"] == "o1"
    r.cancel_order_v2("o1", market_ticker="M")
    assert seen[1][:2] == ("POST", "/trade-api/v2/portfolio/events/orders")
    assert seen[2][:3] == ("DELETE", "/trade-api/v2/portfolio/events/orders/o1", {"market_ticker": "M"})
    # the legacy response key is still understood, the upstream bug (empty book) cannot recur
    assert parse_orderbook_response({"orderbook_fp": {"yes_dollars": [["0.01", "1"]], "no_dollars": []}})[0] == [(100, 1.0)]


# ---------------------------------------------------------------- config / hosts
def test_hosts_default_to_recommended_demo_and_guard_prod():
    s = load_settings(env={})
    assert s.kalshi.env == "demo" and s.kalshi.rest_base == DEMO_REST and s.kalshi.ws_base == DEMO_WS
    assert s.trading_mode == "paper"
    assert load_settings(env={"KALSHI_HOST_PROFILE": "legacy"}).kalshi.rest_base == LEGACY_DEMO_REST
    assert is_demo_url(DEMO_REST) and is_demo_url(LEGACY_DEMO_REST) and not is_demo_url(PROD_REST)
    with pytest.raises(ValueError, match="not a Kalshi demo host"):
        load_settings(env={"KALSHI_REST_URL": PROD_REST})          # demo env pointed at prod -> refuse
    assert load_settings(env={"KALSHI_ENV": "prod"}).kalshi.rest_base == PROD_REST


# ---------------------------------------------------------------- discovery / fees
def test_discovery_handles_removed_and_deprecated_fields():
    from datetime import datetime, timedelta, timezone
    close = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    base = {"ticker": "A", "event_ticker": "E", "status": "active", "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.42", "close_time": close, "exchange_index": 2}
    a = market_from_api({**base, "volume_fp": "500.00"})                  # no volume_24h in current schema
    b = market_from_api({**base, "ticker": "B", "volume_fp": "900.00", "liquidity_dollars": None})
    assert a.volume_24h is None and a.activity == 500.0 and a.exchange_index == 2
    assert [m.ticker for m in rank_for_recording([a, b])] == ["B", "A"]


def test_combo_maker_fee_type_charges_makers():
    fm = FeeModel()
    fm.register_series("KXC", "quadratic_with_combo_maker_fees", 1.0)
    assert fm.fee("KXC-1", 5000, 100, is_taker=False) == pytest.approx(0.44)
