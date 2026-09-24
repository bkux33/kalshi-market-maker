"""End-to-end tests of the DEMO tooling against a local mock of the current Kalshi API.

These prove the client, recorder, book engine, replay and order harness work together
with current-format messages. They are NOT evidence of connectivity to Kalshi itself.
"""

import asyncio
import json
import os
import time

import pytest

from alphalab.core.config import load_settings
from alphalab.data.db import Database
from alphalab.data.ingest import ingest_tape
from alphalab.data.tape import TapeWriter, closed_tape_files, recover_open_files
from alphalab.kalshi.auth import KalshiSigner
from alphalab.kalshi.discovery import find_markets
from alphalab.kalshi.healthcheck import format_report, run_check
from alphalab.kalshi.rest import KalshiREST
from alphalab.sim.backtest import BacktestConfig, run_backtest
from alphalab.sim.replay import ReplaySource, ReplaySpec
from alphalab.strategies import get_strategy
from tests.mock_kalshi import MockKalshi

REST_BASE = "http://127.0.0.1:9/trade-api/v2"


@pytest.fixture
def mock_env(rsa_key, tmp_path):
    key, path = rsa_key
    mock = MockKalshi(key.public_key())
    signer = KalshiSigner.from_file("demo-key-id", str(path))
    rest = KalshiREST(REST_BASE, signer, requests_per_second=1000, transport=mock.transport(), sleep=lambda s: None)
    settings = load_settings(env={"DATA_DIR": str(tmp_path / "data"), "KALSHI_REST_URL": REST_BASE,
                                  "KALSHI_WS_URL": "ws://127.0.0.1:1/trade-api/ws/v2"})
    return mock, signer, rest, settings


async def _with_server(mock, coro_fn):
    import websockets
    async with websockets.serve(mock.ws_handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        return await coro_fn(f"ws://127.0.0.1:{port}/trade-api/ws/v2")


def test_market_discovery_filters(mock_env):
    mock, signer, rest, _ = mock_env
    allm = find_markets(rest, status="open")
    assert [m.ticker for m in allm][:2] == ["KXBTC15M-26SEP241500-00", "KXETH15M-26SEP241500-00"]  # by volume
    assert {m.category for m in allm} == {"Crypto", "Economics"}
    assert [m.ticker for m in find_markets(rest, search="bitcoin")] == []           # titles say BTC
    assert [m.ticker for m in find_markets(rest, search="btc")] == ["KXBTC15M-26SEP241500-00"]
    assert [m.ticker for m in find_markets(rest, category="Economics")] == ["KXCPI-26OCT-T3.0"]
    assert [m.ticker for m in find_markets(rest, max_seconds_to_close=7200)] == \
        ["KXBTC15M-26SEP241500-00", "KXETH15M-26SEP241500-00"]
    assert find_markets(rest, min_volume=1000)[0].ticker == "KXBTC15M-26SEP241500-00"
    assert all(m.spread is not None for m in find_markets(rest, two_sided=True))


def test_health_check_rebuilds_books_matching_rest(mock_env, tmp_path):
    mock, signer, rest, settings = mock_env
    tape = TapeWriter(tmp_path / "raw")
    markets = ["KXBTC15M-26SEP241500-00", "KXETH15M-26SEP241500-00"]
    rep = asyncio.run(_with_server(mock, lambda url: run_check(rest, signer, url, markets, 1.5, "DEMO(mock)", tape=tape)))
    tape.close()
    txt = format_report(rep)
    assert rep["ok"] and txt.startswith("CONNECTED") and "demo-key-id" not in txt and "PRIVATE" not in txt
    assert rep["rest_public"]["ok"] and rep["rest_auth"]["ok"] and mock.auth_failures == 0
    assert rep["ws"]["sequence_gaps"] == 0 and rep["ws"]["duplicates"] == 0
    for m in markets:
        assert rep["markets"][m]["snapshots"] == 1 and rep["markets"][m]["deltas"] > 0
        assert rep["markets"][m]["rest_crosscheck"] == "match", rep["markets"][m]


def test_faults_duplicates_gap_malformed_disconnect(mock_env):
    mock, signer, rest, _ = mock_env
    mock.fault = {"duplicate_every": 4, "gap_at": 9, "malformed_at": 3, "drop_after": 22}
    mock.stream_deltas = 40
    markets = ["KXBTC15M-26SEP241500-00"]
    rep = asyncio.run(_with_server(mock, lambda url: run_check(rest, signer, url, markets, 3.0, "DEMO(mock)")))
    ws = rep["ws"]
    assert ws["duplicates"] >= 1 and ws["malformed"] >= 1 and ws["sequence_gaps"] >= 1
    assert ws["reconnects"] >= 1 and ws["connections"] >= 2
    info = rep["markets"][markets[0]]
    # after resubscribe/reconnect the book is rebuilt from a fresh snapshot and matches the exchange
    assert info["synced"] and info["rest_crosscheck"] == "match", info


def test_record_ingest_replay_deterministic_and_restart_recovery(mock_env, tmp_path):
    mock, signer, rest, _ = mock_env
    raw = tmp_path / "raw"
    markets = ["KXBTC15M-26SEP241500-00", "KXETH15M-26SEP241500-00"]
    tape = TapeWriter(raw)
    for m in markets:
        tape.write("market", rest.get_market(m))
    asyncio.run(_with_server(mock, lambda url: run_check(rest, signer, url, markets, 1.5, "DEMO(mock)", tape=tape)))
    # simulate a crash: the file is left ".open" with a torn final line
    tape._fh.write('{"r": 1, "c": "ws", "f": "{\\"type\\": \\"orderbook_del')
    tape._fh.flush()
    open_path = tape._path
    assert closed_tape_files(raw) == []
    rec = recover_open_files(raw)
    assert len(rec) == 1 and not os.path.exists(open_path)
    db = Database(":memory:")
    st = ingest_tape(db, raw)
    assert st["files"] == 1 and st["events"] > 0
    ev = db.query_df("SELECT kind, COUNT(*) n, MIN(ts_ns) lo FROM book_events GROUP BY 1").set_index("kind")
    assert ev.loc["snapshot", "n"] == 2 and ev.loc["delta", "n"] > 0
    assert 1.7e18 < ev["lo"].min() < 2.2e18          # epoch nanoseconds, not micro/milli
    assert db.query_df("SELECT COUNT(*) n FROM trades")["n"].iloc[0] > 0
    # reconstructed final book == exchange state
    tob = db.query_df("SELECT market, bid, ask FROM tob t WHERE ts_ns = (SELECT MAX(ts_ns) FROM tob WHERE market=t.market)")
    for r in tob.itertuples():
        yes, no = rest.get_orderbook(r.market)
        assert r.bid == max(p for p, _ in yes) and r.ask == 10000 - max(p for p, _ in no)
    # deterministic replay: identical results twice, for several strategies
    for name, params in (("imbalance", {"min_tts_s": 0, "horizon_s": 1}), ("market_maker", {"min_tts_s": 0}),
                         ("momentum", {"min_tts_s": 0, "min_move_c": 0.5})):
        cls = get_strategy(name)
        r1 = run_backtest(db, lambda: cls(**params), BacktestConfig(replay=ReplaySpec(markets=markets)), persist=False)
        r2 = run_backtest(db, lambda: cls(**params), BacktestConfig(replay=ReplaySpec(markets=markets)), persist=False)
        assert r1.result_hash == r2.result_hash and r1.metrics["events"] == r2.metrics["events"] > 0


def test_demo_order_path_against_mock(mock_env):
    from alphalab.execution.demo_orders import format_demo_orders, run_demo_orders
    mock, signer, rest, settings = mock_env
    # top ask 32c x1 (full fill), then 33c x2 so a 3-lot IOC can only partially fill
    mock.markets["KXETH15M-26SEP241500-00"]["no"] = {6800: 1.0, 6700: 2.0, 6600: 30.0}
    mock.stream_deltas = 0  # static book so fills are deterministic
    rep = asyncio.run(_with_server(mock, lambda url: run_demo_orders(
        settings, rest, signer, url, "KXETH15M-26SEP241500-00", attempt_fill=True, timeout=5.0)))
    out = format_demo_orders(rep)
    steps = rep["steps"]
    for k in ("market_data", "create_and_ack", "local_duplicate_block", "exchange_duplicate_block",
              "reconnect_with_order", "cancel_and_ack", "stale_price_block", "kill_switch", "fill", "partial_fill"):
        assert steps[k]["result"] == "pass", (k, steps[k], out)
    assert steps["partial_fill"]["filled"] == 2.0 and steps["partial_fill"]["requested"] == 3.0
    assert rep["ok"] and not [o for o in mock.orders.values() if o["status"] == "resting"]  # nothing left resting
    # every order request used the V2 endpoint and schema
    posts = [r for r in mock.requests if r[0] == "POST"]
    assert posts and all(r[1] == "/trade-api/v2/portfolio/events/orders" for r in posts)


def test_demo_orders_refuses_non_demo(rsa_key, tmp_path):
    from alphalab.execution.demo_orders import DemoOnlyError, assert_demo
    prod = load_settings(env={"KALSHI_ENV": "prod", "DATA_DIR": str(tmp_path)})
    with pytest.raises(DemoOnlyError):
        assert_demo(prod, prod.kalshi.rest_base, prod.kalshi.ws_base)
    demo = load_settings(env={"DATA_DIR": str(tmp_path)})
    with pytest.raises(DemoOnlyError):
        assert_demo(demo, "https://external-api.kalshi.com/trade-api/v2", demo.kalshi.ws_base)
    live = load_settings(env={"DATA_DIR": str(tmp_path), "TRADING_MODE": "live"})
    with pytest.raises(DemoOnlyError, match="TRADING_MODE=paper"):
        assert_demo(live, live.kalshi.rest_base, live.kalshi.ws_base)


def test_data_quality_audit_counts_faults(mock_env, tmp_path):
    from alphalab.data.quality import audit_tape, render_markdown
    mock, signer, rest, _ = mock_env
    mock.fault = {"duplicate_every": 5, "gap_at": 9, "malformed_at": 3, "drop_after": 25}
    mock.stream_deltas = 40
    raw = tmp_path / "raw"
    tape = TapeWriter(raw)
    asyncio.run(_with_server(mock, lambda url: run_check(rest, signer, url, ["KXBTC15M-26SEP241500-00"], 3.0,
                                                          "DEMO(mock)", tape=tape)))
    tape.close()
    r = audit_tape(raw)
    assert r["markets_recorded"] == 1 and r["orderbook_snapshots"] >= 2 and r["orderbook_deltas"] > 0
    assert r["sequence_gaps"] >= 1 and r["exact_duplicate_frames"] >= 1 and r["malformed_ws_frames"] >= 1
    assert r["missing_receive_timestamps"] == 0 and r["missing_exchange_timestamps"] == 0
    assert r["crossed_book_observations"] == 0 and r["books_synced_at_end"] == 1
    assert 90 < r["valid_record_pct"] < 100
    assert r["connections"] >= 2 and r["reconnects"] >= 1
    assert r["collection_start_utc"].startswith("20") and 0.5 < r["duration_s"] < 60
    md = render_markdown(r)
    assert "Sequence gaps" in md and "Valid records" in md
