"""DuckDB storage for market data, simulations, experiments and risk events.

Design notes
------------
* Raw WebSocket frames are captured first to append-only JSONL tape files
  (``alphalab.data.tape``); ``ingest`` loads *closed* tape files into DuckDB
  in one transaction per file and records the file in ``ingested_files`` so a
  file is never loaded twice (idempotent, crash-safe).
* DuckDB allows one writer process at a time. Long-running services (recorder,
  paper trader) therefore write JSONL, and short CLI jobs (ingest, backtest,
  experiments) take the write lock briefly. ``connect`` retries on lock
  contention; the dashboard opens read-only connections.
* All timestamps are ``BIGINT`` epoch nanoseconds (``*_ns``) or epoch seconds
  (``*_ts``, DOUBLE) as named. Prices are integer units of $0.0001.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import duckdb
import pandas as pd

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta(key VARCHAR PRIMARY KEY, value VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS ingested_files(
        path VARCHAR PRIMARY KEY, size BIGINT, lines BIGINT, events BIGINT, ingested_ns BIGINT)""",
    """CREATE TABLE IF NOT EXISTS book_events(
        ts_ns BIGINT NOT NULL, ord BIGINT NOT NULL, market VARCHAR NOT NULL, kind VARCHAR NOT NULL,
        seq BIGINT, sid INTEGER, side VARCHAR, price INTEGER, delta DOUBLE,
        yes_levels VARCHAR, no_levels VARCHAR, source VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS trades(
        ts_ns BIGINT NOT NULL, ord BIGINT NOT NULL, market VARCHAR NOT NULL, trade_id VARCHAR,
        price INTEGER NOT NULL, qty DOUBLE NOT NULL, taker_side VARCHAR, exch_ts_ns BIGINT, source VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS tob(
        ts_ns BIGINT NOT NULL, market VARCHAR NOT NULL, bid INTEGER, ask INTEGER, bid_qty DOUBLE,
        ask_qty DOUBLE, mid DOUBLE, spread INTEGER, bid_depth5 DOUBLE, ask_depth5 DOUBLE,
        imbalance1 DOUBLE, imbalance5 DOUBLE, microprice DOUBLE, synced BOOLEAN)""",
    """CREATE TABLE IF NOT EXISTS markets(
        ticker VARCHAR PRIMARY KEY, event_ticker VARCHAR, series_ticker VARCHAR, title VARCHAR,
        status VARCHAR, market_type VARCHAR, open_ts DOUBLE, close_ts DOUBLE,
        expected_expiration_ts DOUBLE, strike_type VARCHAR, floor_strike DOUBLE, cap_strike DOUBLE,
        result VARCHAR, settlement_value DOUBLE, price_level_structure VARCHAR, yes_bid INTEGER,
        yes_ask INTEGER, volume DOUBLE, volume_24h DOUBLE, open_interest DOUBLE, liquidity_usd DOUBLE,
        can_close_early BOOLEAN, mutually_exclusive BOOLEAN, fee_type VARCHAR, fee_multiplier DOUBLE,
        depth_quality VARCHAR, underlying VARCHAR, source VARCHAR, updated_ns BIGINT)""",
    """CREATE TABLE IF NOT EXISTS settlements(
        market VARCHAR PRIMARY KEY, ts_ns BIGINT, value DOUBLE, result VARCHAR, inferred BOOLEAN, source VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS lifecycle(ts_ns BIGINT, market VARCHAR, event_type VARCHAR, data VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS external_prices(
        ts_ns BIGINT NOT NULL, ord BIGINT NOT NULL, symbol VARCHAR NOT NULL, price DOUBLE NOT NULL,
        bid DOUBLE, ask DOUBLE, source VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS runs(
        run_id VARCHAR PRIMARY KEY, kind VARCHAR, strategy VARCHAR, params VARCHAR, config VARCHAR,
        data_spec VARCHAR, data_hash VARCHAR, created_ns BIGINT, code_version VARCHAR,
        metrics VARCHAR, experiment_id VARCHAR, phase VARCHAR, result_hash VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS signals(
        run_id VARCHAR, ts_ns BIGINT, strategy VARCHAR, market VARCHAR, name VARCHAR, value DOUBLE,
        payload VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS orders(
        run_id VARCHAR, mode VARCHAR, order_id VARCHAR, client_order_id VARCHAR, strategy VARCHAR,
        market VARCHAR, action VARCHAR, price INTEGER, qty DOUBLE, tif VARCHAR, post_only BOOLEAN,
        ts_decision BIGINT, ts_submit BIGINT, ts_active BIGINT, ts_done BIGINT, status VARCHAR,
        filled_qty DOUBLE, avg_fill_price DOUBLE, reason VARCHAR, tag VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS fills(
        run_id VARCHAR, mode VARCHAR, fill_id VARCHAR, order_id VARCHAR, strategy VARCHAR,
        market VARCHAR, ts_ns BIGINT, action VARCHAR, price INTEGER, qty DOUBLE, liquidity VARCHAR,
        fee DOUBLE, mid_at_fill DOUBLE, slippage_usd DOUBLE, position_after DOUBLE,
        markout_1s DOUBLE, markout_5s DOUBLE, markout_30s DOUBLE, markout_60s DOUBLE, tag VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS round_trips(
        run_id VARCHAR, strategy VARCHAR, market VARCHAR, direction INTEGER, qty DOUBLE,
        entry_ns BIGINT, exit_ns BIGINT, entry_price DOUBLE, exit_price DOUBLE, gross DOUBLE,
        fees DOUBLE, net DOUBLE, exit_reason VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS pnl(
        run_id VARCHAR, ts_ns BIGINT, cash DOUBLE, realized DOUBLE, unrealized DOUBLE, fees DOUBLE,
        equity DOUBLE, gross_exposure DOUBLE, positions VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS experiments(
        experiment_id VARCHAR PRIMARY KEY, name VARCHAR, strategy VARCHAR, created_ns BIGINT,
        spec VARCHAR, status VARCHAR, classification VARCHAR, flags VARCHAR, summary VARCHAR,
        selected_params VARCHAR, test_evaluations INTEGER, data_hash VARCHAR, n_trials INTEGER)""",
    """CREATE TABLE IF NOT EXISTS experiment_results(
        experiment_id VARCHAR, run_id VARCHAR, phase VARCHAR, fold INTEGER, params VARCHAR,
        metrics VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS predictive_results(
        study_id VARCHAR, created_ns BIGINT, feature VARCHAR, horizon_s DOUBLE, split VARCHAR,
        n BIGINT, ic DOUBLE, ic_tstat DOUBLE, hit_rate DOUBLE, top_bucket_move DOUBLE,
        bottom_bucket_move DOUBLE, cost_hurdle DOUBLE, net_edge_top DOUBLE, net_edge_bottom DOUBLE,
        data_hash VARCHAR, notes VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS risk_events(
        ts_ns BIGINT, mode VARCHAR, run_id VARCHAR, type VARCHAR, severity VARCHAR, detail VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS strategy_status(
        strategy_key VARCHAR PRIMARY KEY, strategy VARCHAR, params VARCHAR, status VARCHAR,
        experiment_id VARCHAR, updated_ns BIGINT, reason VARCHAR)""",
    """CREATE INDEX IF NOT EXISTS idx_book_market_ts ON book_events(market, ts_ns)""",
    """CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market, ts_ns)""",
    """CREATE INDEX IF NOT EXISTS idx_tob_market_ts ON tob(market, ts_ns)""",
]

MARKET_COLUMNS = [
    "ticker", "event_ticker", "series_ticker", "title", "status", "market_type", "open_ts", "close_ts",
    "expected_expiration_ts", "strike_type", "floor_strike", "cap_strike", "result", "settlement_value",
    "price_level_structure", "yes_bid", "yes_ask", "volume", "volume_24h", "open_interest",
    "liquidity_usd", "can_close_early", "mutually_exclusive", "fee_type", "fee_multiplier",
    "depth_quality", "underlying", "source", "updated_ns"]


class Database:
    def __init__(self, path: str | Path, read_only: bool = False, lock_timeout_s: float = 30.0):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.read_only = read_only
        deadline = time.time() + lock_timeout_s
        while True:
            try:
                self.con = duckdb.connect(self.path, read_only=read_only)
                break
            except duckdb.IOException as exc:
                if "lock" not in str(exc).lower() or time.time() > deadline:
                    raise
                time.sleep(0.25)
        if not read_only:
            self.init_schema()

    # ------------------------------------------------------------------ schema
    def init_schema(self) -> None:
        for stmt in SCHEMA:
            self.con.execute(stmt)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)])

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ generic
    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        return self.con.execute(sql, params or [])

    def query_df(self, sql: str, params: Optional[Sequence[Any]] = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        return self.insert_df(table, pd.DataFrame(rows))

    def insert_df(self, table: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        self.con.register("_ins_df", df)
        try:
            self.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _ins_df")
        finally:
            self.con.unregister("_ins_df")
        return len(df)

    def next_ord(self, table: str) -> int:
        r = self.con.execute(f"SELECT COALESCE(MAX(ord), -1) + 1 FROM {table}").fetchone()
        return int(r[0])

    def begin(self) -> None:
        self.con.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.con.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.con.execute("ROLLBACK")
        except Exception:
            pass

    # ------------------------------------------------------------------ markets
    def upsert_markets(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Insert or merge market metadata; non-null new values win."""
        n = 0
        for r in rows:
            row = {c: r.get(c) for c in MARKET_COLUMNS}
            row["updated_ns"] = row["updated_ns"] or time.time_ns()
            existing = self.con.execute("SELECT * FROM markets WHERE ticker = ?", [row["ticker"]]).df()
            if not existing.empty:
                old = existing.iloc[0].to_dict()
                for c in MARKET_COLUMNS:
                    if row[c] is None and old.get(c) is not None and not (isinstance(old.get(c), float) and pd.isna(old.get(c))):
                        row[c] = old[c]
                self.con.execute("DELETE FROM markets WHERE ticker = ?", [row["ticker"]])
            cols = ",".join(MARKET_COLUMNS)
            qs = ",".join("?" for _ in MARKET_COLUMNS)
            self.con.execute(f"INSERT INTO markets ({cols}) VALUES ({qs})", [row[c] for c in MARKET_COLUMNS])
            n += 1
        return n

    def upsert_settlement(self, market: str, ts_ns: int, value: float, result: str,
                          inferred: bool, source: str) -> None:
        cur = self.con.execute("SELECT inferred FROM settlements WHERE market = ?", [market]).fetchone()
        if cur is not None and cur[0] is False and inferred:
            return  # never overwrite an official settlement with an inferred one
        self.con.execute("DELETE FROM settlements WHERE market = ?", [market])
        self.con.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?)",
                         [market, ts_ns, value, result, inferred, source])
        self.con.execute("UPDATE markets SET result = ?, settlement_value = ? WHERE ticker = ?",
                         [result, value, market])

    def markets_df(self) -> pd.DataFrame:
        return self.query_df("SELECT * FROM markets ORDER BY close_ts NULLS LAST, ticker")

    # ------------------------------------------------------------------ integrity
    def integrity_report(self) -> Dict[str, Any]:
        q = self.con.execute
        rep: Dict[str, Any] = {}
        for t in ("book_events", "trades", "tob", "markets", "settlements", "external_prices",
                  "runs", "orders", "fills", "experiments"):
            rep[f"rows_{t}"] = int(q(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        rep["dup_trade_ids"] = int(q("""SELECT COUNT(*) FROM (SELECT market, trade_id, COUNT(*) c FROM trades
                                        WHERE trade_id IS NOT NULL AND trade_id <> '' GROUP BY 1,2 HAVING c > 1)""").fetchone()[0])
        rep["bad_prices_book"] = int(q("""SELECT COUNT(*) FROM book_events WHERE kind='delta'
                                          AND (price <= 0 OR price >= 10000)""").fetchone()[0])
        rep["bad_prices_trades"] = int(q("SELECT COUNT(*) FROM trades WHERE price <= 0 OR price >= 10000 OR qty <= 0").fetchone()[0])
        rep["crossed_tob"] = int(q("SELECT COUNT(*) FROM tob WHERE bid >= ask").fetchone()[0])
        rep["orphan_fills"] = int(q("""SELECT COUNT(*) FROM fills f LEFT JOIN orders o
                                       ON f.run_id = o.run_id AND f.order_id = o.order_id WHERE o.order_id IS NULL""").fetchone()[0])
        rep["settlements_inferred"] = int(q("SELECT COUNT(*) FROM settlements WHERE inferred").fetchone()[0])
        return rep


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
