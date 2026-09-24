import json
import os
import time

import pandas as pd
import pytest

from alphalab.core.events import BookSnapshot
from alphalab.data.importers import import_baseline_csv, import_crypto_sample_csv
from alphalab.data.ingest import ingest_tape
from alphalab.data.synthetic import SyntheticSpec, write_synthetic
from alphalab.data.tape import TapeWriter, closed_tape_files, read_tape
from alphalab.execution.journal import ingest_journals
from alphalab.execution.killswitch import KillSwitch
from alphalab.execution.session import TradingSession, run_replay_feed
from alphalab.sim.replay import ReplaySource, ReplaySpec
from alphalab.strategies import get_strategy
from tests.conftest import C


# ---------------------------------------------------------------- tape + ingest
def _frames(n0=1):
    return [
        {"type": "orderbook_snapshot", "sid": 1, "seq": n0, "msg": {"market_ticker": "KXT-1",
         "yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "10"]]}},
        {"type": "orderbook_delta", "sid": 1, "seq": n0 + 1, "msg": {"market_ticker": "KXT-1",
         "price_dollars": "0.41", "delta_fp": "5", "side": "yes"}},
        {"type": "trade", "sid": 2, "msg": {"market_ticker": "KXT-1", "yes_price_dollars": "0.45",
         "count_fp": "2", "taker_side": "yes", "trade_id": "tr1"}},
    ]


def test_tape_roundtrip_rotation_and_idempotent_ingest(db, tmp_path):
    t = TapeWriter(tmp_path / "raw")
    base = 1_767_225_600_000_000_000
    for i, f in enumerate(_frames()):
        t.write("ws", json.dumps(f), base + i)
    t.write("market", {"ticker": "KXT-1", "event_ticker": "KXT", "status": "settled", "result": "yes",
                       "close_time": "2026-01-01T00:10:00Z"}, base + 10)
    assert closed_tape_files(tmp_path / "raw") == []  # still open -> not ingestable
    t.write("ws", json.dumps(_frames(3)[0]), base + 3600 * 10**9)  # next hour -> rotation closes first file
    t.close()
    files = closed_tape_files(tmp_path / "raw")
    assert len(files) == 2
    with open(files[0], "a") as fh:
        fh.write('{"r": 1, "c": "ws", "f": "trunc')  # simulated crash mid-line
    assert len(list(read_tape(files[0]))) == 4
    s1 = ingest_tape(db, tmp_path / "raw")
    s2 = ingest_tape(db, tmp_path / "raw")
    assert s1["files"] == 2 and s2["files"] == 0 and s2["skipped"] == 2
    rep = db.integrity_report()
    assert rep["rows_trades"] == 1 and rep["rows_book_events"] == 3 and rep["dup_trade_ids"] == 0
    assert db.query_df("SELECT result, inferred FROM settlements").iloc[0].tolist() == ["yes", False]
    tob = db.query_df("SELECT * FROM tob WHERE market='KXT-1' ORDER BY ts_ns")
    assert tob["bid"].iloc[-1] == 4000 and tob["ask"].iloc[0] == 4500


def test_official_settlement_not_overwritten_by_inferred(db):
    db.upsert_markets([{"ticker": "M"}])
    db.upsert_settlement("M", 1, 1.0, "yes", False, "api")
    db.upsert_settlement("M", 2, 0.0, "no", True, "csv")
    assert db.query_df("SELECT result FROM settlements")["result"].iloc[0] == "yes"


def test_csv_importers(db, tmp_path):
    base = pd.DataFrame({
        "binance_id": [1, 2], "binance_received_at": ["2026-03-02 14:46:12.5-05:00", "2026-03-02 14:46:13.5-05:00"],
        "binance_event_time_ms": [0, 0], "binance_symbol": ["ETHUSDT"] * 2, "binance_asset": ["ETH"] * 2,
        "binance_best_bid": [2000.0, 2001.0], "binance_best_bid_qty": [1, 1], "binance_best_ask": [2000.5, 2001.5],
        "binance_best_ask_qty": [1, 1], "binance_mid": [2000.25, 2001.25], "binance_spread": [0.5, 0.5],
        "kalshi_id": [1, 2], "kalshi_fetched_at": ["2026-03-02 14:46:12.4-05:00", "2026-03-02 14:46:13.4-05:00"],
        "kalshi_market_ticker": ["KXETH15M-26MAR021500-00"] * 2, "kalshi_asset": ["ETH"] * 2,
        "kalshi_yes_best_bid_dollars": [0.46, 0.46], "kalshi_yes_best_bid_qty": [6, 6],
        "kalshi_no_best_bid_dollars": [0.52, 0.52], "kalshi_no_best_bid_qty": [5, 5],
        "kalshi_mid_dollars": [0.47, 0.47], "kalshi_spread_dollars": [0.02, 0.02],
        "kalshi_yes_bids_json": ["[[45, 3], [46, 6]]"] * 2, "kalshi_no_bids_json": ["[[51, 2], [52, 5]]"] * 2,
        "kalshi_resolution": ["yes", "yes"]})
    p = tmp_path / "b.csv"
    base.to_csv(p, index=False)
    out = import_baseline_csv(db, p)
    assert out["markets"] == 1 and out["events"] >= 3
    ts = db.query_df("SELECT MIN(ts_ns) lo FROM book_events")["lo"].iloc[0]
    assert ts == 1772480772400000000  # 2026-03-02 14:46:12.4-05:00 in epoch *nanoseconds*
    assert db.query_df("SELECT MIN(ts_ns) lo FROM external_prices")["lo"].iloc[0] == 1772480772500000000
    t = db.query_df("SELECT bid, ask, bid_qty, ask_qty FROM tob").iloc[0].tolist()
    assert t == [4600, 4800, 6.0, 5.0]
    assert db.query_df("SELECT inferred FROM settlements")["inferred"].iloc[0]
    cs = pd.DataFrame({"ts": ["x", "y"], "ts_unix": [1771993365.0, 1771993367.0],
                       "btc_ticker": ["KXBTC15M-26FEB242330-30"] * 2, "btc_yes_bid": [86, 99], "btc_yes_ask": [87, 100],
                       "btc_floor_strike": [65000.0] * 2, "btc_time_to_expiry": [10.0, 0.0], "btc_real_price": [65100.0] * 2})
    p2 = tmp_path / "c.csv"
    cs.to_csv(p2, index=False)
    out2 = import_crypto_sample_csv(db, p2)
    assert out2["markets"] == 1
    m = db.query_df("SELECT depth_quality, floor_strike, result FROM markets WHERE ticker LIKE 'KXBTC%'").iloc[0]
    assert m["depth_quality"].startswith("top_only") and m["floor_strike"] == 65000.0 and m["result"] == "yes"


# ---------------------------------------------------------------- paper session
def _session(settings, db, strategy="market_maker", **params):
    src = ReplaySource(db, ReplaySpec()).materialize()
    cls = get_strategy(strategy)
    return TradingSession(settings, [cls(**params)], mode="paper", market_meta=src.meta), src


def test_paper_replay_session_journal_state_and_ingest(db, settings):
    write_synthetic(db, SyntheticSpec(n_markets=2, seed=8))
    sess, src = _session(settings, db)
    m = run_replay_feed(sess, src)
    assert m["n_fills"] > 0 and m["net_pnl"] == pytest.approx(m["gross_pnl"] - m["fees"])
    journals = list(settings.data.journal_dir.glob("*.jsonl"))
    assert len(journals) == 1 and not list(settings.data.journal_dir.glob("*.active"))
    state = json.loads((settings.data.state_dir / "paper_state.json").read_text())
    assert state["run_id"] == sess.run_id and "risk" in state and "books" in state
    out = ingest_journals(db, settings.data.journal_dir)
    assert out["fills"] == m["n_fills"]
    assert ingest_journals(db, settings.data.journal_dir)["files"] == 0  # idempotent
    assert db.integrity_report()["orphan_fills"] == 0


def test_kill_switch_mid_session_cancels_and_halts(db, settings):
    write_synthetic(db, SyntheticSpec(n_markets=1, seed=8))
    sess, src = _session(settings, db)
    events = list(src)
    for ev in events[:400]:
        sess.tick(ev.ts_ns)
        sess.on_event(ev)
    assert sess.broker.open_orders(), "market maker should have resting quotes"
    KillSwitch(settings.risk.kill_switch_file).trip("test")
    sess.kill._last_check = 0
    now = events[400].ts_ns
    sess.tick(now)
    sess.tick(now + 1_000_000_000)
    assert not sess.broker.open_orders() and sess.risk.halted
    before = sess.broker.stats["submitted"]
    for ev in events[400:800]:
        sess.tick(ev.ts_ns)
        sess.on_event(ev)
    assert sess.broker.stats["submitted"] == before  # nothing new after the kill switch
    sess.stop()


def test_stale_data_and_disconnect_cancel_orders(db, settings):
    write_synthetic(db, SyntheticSpec(n_markets=1, seed=8))
    sess, src = _session(settings, db)
    events = list(src)
    for ev in events[:300]:
        sess.tick(ev.ts_ns)
        sess.on_event(ev)
    last = events[299].ts_ns
    from alphalab.sim.broker import OrderRequest
    o = sess.broker.submit(OrderRequest("manual", "KXSYN15M-SYN000", "buy", 2 * C, 1), last)
    sess.tick(last + 1_000_000_000)
    assert o.status == "resting"
    sess.tick(last + 10 * 1_000_000_000)      # no data for 10s > stale_data_s
    sess.tick(last + 11 * 1_000_000_000)
    assert o.status == "canceled" and not sess.broker.open_orders()
    assert any(r["type"] == "stale_data_cancel" for r in sess.recent["risk"])
    sess.on_connection(False, last + 12 * 1_000_000_000)
    assert not sess.risk.connected
    sess.stop()


# ---------------------------------------------------------------- dashboard + assistant
def test_dashboard_endpoints_and_token(db, settings, monkeypatch):
    from fastapi.testclient import TestClient
    from alphalab.dashboard.app import create_app
    from alphalab.data.db import Database
    d = Database(settings.data.db_path)
    write_synthetic(d, SyntheticSpec(n_markets=1, seed=1))
    d.close()
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    c = TestClient(create_app(settings))
    assert c.get("/api/overview").status_code == 401
    h = {"Authorization": "Bearer secret"}
    for ep in ("overview", "markets", "strategies", "experiments", "runs", "trades", "risk", "performance",
               "settings", "state/paper"):
        assert c.get(f"/api/{ep}", headers=h).status_code == 200, ep
    for page in ("dashboard", "markets", "experiments", "risk"):
        assert c.get(f"/{page}").status_code == 200
    r = c.post("/api/kill", headers={**h, "content-type": "application/json"}, json={"reason": "t"})
    assert r.json()["tripped"] and KillSwitch(settings.risk.kill_switch_file).is_tripped(force=True)
    assert c.post("/api/kill/reset", headers=h).status_code in (404, 405)  # reset is CLI-only


def test_assistant_is_read_only_and_offline_mode_works(db):
    from alphalab.research import assistant
    names = {t["name"] for t in assistant.TOOLS}
    assert not any(w in n for n in names for w in ("order", "place", "cancel", "trade", "live", "status"))
    src = open(assistant.__file__).read()
    assert "alphalab.execution" not in src and "alphalab.kalshi" not in src
    out = assistant.answer_offline(db, "is it concentrated?")
    assert "data_inventory" in out
    assert json.loads(assistant.run_tool(db, "nope", {}))["error"]
