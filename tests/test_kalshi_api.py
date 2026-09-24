import asyncio
import json

import httpx
import pytest

from alphalab.kalshi.auth import KalshiSigner
from alphalab.kalshi.discovery import crypto15m_close_from_ticker, market_from_api, rank_for_recording
from alphalab.kalshi.messages import parse_frame
from alphalab.kalshi.rest import KalshiAPIError, KalshiREST, parse_orderbook_response
from alphalab.kalshi.ws import KalshiWebSocket

BASE = "https://demo-api.kalshi.co/trade-api/v2"


def _soon():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def rest_with(handler, signer=None):
    return KalshiREST(BASE, signer, requests_per_second=1000, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None)


def test_orderbook_fixed_point_and_legacy():
    fp = {"orderbook_fp": {"yes_dollars": [["0.4500", "10.00"]], "no_dollars": [["0.5000", "7.50"]]}}
    assert parse_orderbook_response(fp) == ([(4500, 10.0)], [(5000, 7.5)])
    legacy = {"orderbook": {"yes": [[45, 10]], "no": [[50, 7]]}}
    assert parse_orderbook_response(legacy) == ([(4500, 10.0)], [(5000, 7.0)])
    assert parse_orderbook_response({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}) == ([], [])


def test_rest_pagination_retry_and_errors(rsa_key):
    calls = {"n": 0}

    def handler(req: httpx.Request):
        calls["n"] += 1
        if req.url.path.endswith("/markets"):
            if req.url.params.get("cursor") is None and calls["n"] == 1:
                return httpx.Response(429, text="slow down")
            if req.url.params.get("cursor") == "c2":
                return httpx.Response(200, json={"markets": [{"ticker": "B"}], "cursor": ""})
            return httpx.Response(200, json={"markets": [{"ticker": "A"}], "cursor": "c2"})
        if req.url.path.endswith("/portfolio/balance"):
            assert "KALSHI-ACCESS-SIGNATURE" in req.headers
            return httpx.Response(200, json={"balance": 100})
        if req.url.path.endswith("/markets/BAD"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(500)

    r = rest_with(handler)
    assert [m["ticker"] for m in r.iter_markets(series_ticker="X")] == ["A", "B"]
    with pytest.raises(KalshiAPIError) as e:
        r.get_market("BAD")
    assert e.value.status == 404
    with pytest.raises(KalshiAPIError):
        r.get_balance()  # no credentials
    key, path = rsa_key
    r2 = rest_with(handler, KalshiSigner.from_file("kid", str(path)))
    assert r2.get_balance() == {"balance": 100}


def test_market_discovery_parsing_and_ranking():
    raw = {"ticker": "KXBTC15M-26MAR021500-00", "event_ticker": "KXBTC15M-26MAR021500", "status": "active",
           "yes_bid_dollars": "0.4500", "yes_ask_dollars": "0.4700", "volume_24h_fp": "1200.00",
           "open_interest_fp": "50.00", "close_time": _soon(), "floor_strike": 65000.5,
           "strike_type": "greater_or_equal", "result": "", "settlement_value_dollars": None}
    m = market_from_api(raw)
    assert (m.yes_bid, m.yes_ask, m.spread, m.series_ticker) == (4500, 4700, 200, "KXBTC15M")
    assert m.floor_strike == 65000.5 and m.volume_24h == 1200.0
    closed = market_from_api({**raw, "ticker": "Z", "close_time": "2000-01-01T00:00:00Z"})
    assert [x.ticker for x in rank_for_recording([m, closed])] == [m.ticker]
    # 15:00 US/Eastern on 2026-03-02 is 20:00 UTC
    assert crypto15m_close_from_ticker("KXETH15M-26MAR021500-00") == pytest.approx(1772481600)


def test_ws_frame_parsing():
    evs = parse_frame({"type": "orderbook_snapshot", "sid": 2, "seq": 2, "msg": {
        "market_ticker": "M", "yes_dollars_fp": [["0.0800", "300.00"]], "no_dollars_fp": [["0.5400", "20.00"]]}}, 10)
    assert evs[0].yes == [(800, 300.0)] and evs[0].no == [(5400, 20.0)] and evs[0].seq == 2
    tr = parse_frame({"type": "trade", "msg": {"market_ticker": "M", "yes_price_dollars": "0.360",
                                                "count_fp": "136.00", "taker_side": "no", "trade_id": "t"}}, 11)[0]
    assert (tr.price, tr.qty, tr.taker_side) == (3600, 136.0, "no")
    st = parse_frame({"type": "market_lifecycle_v2", "msg": {"market_ticker": "M", "event_type": "settled",
                                                              "result": "yes"}}, 12)
    assert st[1].value == 1.0
    assert parse_frame({"type": "ticker", "msg": {}}, 1) == []


# ---------------------------------------------------------------- websocket (real local server)
def test_ws_subscribe_gap_resubscribe_and_reconnect(rsa_key):
    import websockets

    key, path = rsa_key
    signer = KalshiSigner.from_file("kid", str(path))
    seen = {"connections": 0, "subscribes": [], "headers": []}

    async def server(ws):
        seen["connections"] += 1
        seen["headers"].append(dict(ws.request.headers))
        conn = seen["connections"]
        sid = 0
        async for raw in ws:
            cmd = json.loads(raw)
            seen["subscribes"].append((conn, cmd["cmd"], cmd.get("params", {}).get("channels")))
            if cmd["cmd"] != "subscribe":
                continue
            sid += 1
            await ws.send(json.dumps({"id": cmd["id"], "type": "subscribed",
                                      "msg": {"channel": cmd["params"]["channels"][0], "sid": sid}}))
            if cmd["params"]["channels"] == ["orderbook_delta"]:
                await ws.send(json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": 1, "msg": {
                    "market_ticker": "M", "yes_dollars_fp": [["0.40", "5"]], "no_dollars_fp": [["0.55", "5"]]}}))
                if conn == 1 and sid == 1:
                    await ws.send(json.dumps({"type": "orderbook_delta", "sid": sid, "seq": 2, "msg": {
                        "market_ticker": "M", "price_dollars": "0.41", "delta_fp": "3", "side": "yes"}}))
                    await ws.send(json.dumps({"type": "orderbook_delta", "sid": sid, "seq": 5, "msg": {
                        "market_ticker": "M", "price_dollars": "0.42", "delta_fp": "3", "side": "yes"}}))
                elif conn == 1:
                    await ws.close()  # drop the connection after the resync snapshot

    async def main():
        statuses, raws, events = [], [], []
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            client = KalshiWebSocket(f"ws://127.0.0.1:{port}", signer, ["M"],
                                     channels=("orderbook_delta", "trade"),
                                     on_event=events.append, on_raw=lambda r, n: raws.append(r),
                                     on_status=lambda s, i: statuses.append(s), max_backoff_s=0.05)
            task = asyncio.create_task(client.run())
            for _ in range(200):
                await asyncio.sleep(0.02)
                if seen["connections"] >= 2 and client.books.is_synced("M"):
                    break
            await client.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return client, statuses, raws, events

    client, statuses, raws, events = asyncio.run(main())
    assert "KALSHI-ACCESS-SIGNATURE" in {k.upper() for k in seen["headers"][0]}
    assert "gap" in statuses and "resubscribed" in statuses
    assert "disconnected" in statuses and seen["connections"] >= 2
    assert client.stats["gaps"] == 1 and client.stats["reconnects"] >= 1
    assert any(c == (1, "unsubscribe", None) for c in seen["subscribes"])
    assert len(raws) >= 5 and any(e.kind == "snapshot" for e in events)
    assert client.books.book("M").bid_price == 4000  # rebuilt from the fresh snapshot
