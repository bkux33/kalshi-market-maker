"""Importers for third-party CSV samples.

These samples are tiny and of uncertain provenance; they exist so the full
pipeline can be exercised on *real* Kalshi prices. Settlements from them are
always stored with ``inferred = TRUE``. The CSVs themselves are not
redistributed with this project (see DATA.md for their origin and licence
status).

Supported formats
-----------------
``baseline``: ``data/example_data.csv`` from
  rnop/Kalshi-Prediction-Market-Trading-Bot-Public-Baseline - full-depth Kalshi
  snapshots (integer-cent levels) joined with Binance best bid/ask.
``crypto_sample``: ``data/sample_features.csv`` from kapelame/kalshi-crypto-bot -
  2-second top-of-book for BTC/ETH/SOL/XRP 15-minute markets with strike and
  spot reference price but *no depth sizes and no settlement column*.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from alphalab.core.events import BookSnapshot, ExternalPrice, Settlement
from alphalab.core.prices import CENT
from alphalab.data.db import Database
from alphalab.data.ingest import EventSink, derive_tob
from alphalab.kalshi.discovery import crypto15m_close_from_ticker


def _to_ns(series: pd.Series) -> np.ndarray:
    # pandas >= 3 may parse at microsecond resolution; force nanoseconds explicitly.
    return pd.to_datetime(series, utc=True, format="mixed").dt.as_unit("ns").astype("int64").to_numpy()


def import_baseline_csv(db: Database, path: str | Path) -> Dict[str, int]:
    df = pd.read_csv(path)
    source = "import:baseline_csv"
    db.begin()
    try:
        sink = EventSink(db, source)
        k = df.drop_duplicates(["kalshi_market_ticker", "kalshi_fetched_at"]).copy()
        k["ts_ns"] = _to_ns(k["kalshi_fetched_at"])
        k = k.sort_values(["ts_ns", "kalshi_market_ticker"])
        for r in k.itertuples(index=False):
            yes = [(int(p) * CENT, float(q)) for p, q in json.loads(r.kalshi_yes_bids_json)]
            no = [(int(p) * CENT, float(q)) for p, q in json.loads(r.kalshi_no_bids_json)]
            sink.add(BookSnapshot(int(r.ts_ns), r.kalshi_market_ticker, yes, no))
        b = df.drop_duplicates(["binance_received_at", "binance_symbol"]).copy()
        b["ts_ns"] = _to_ns(b["binance_received_at"])
        for r in b.sort_values("ts_ns").itertuples(index=False):
            sym = str(r.binance_asset) + "-USD"
            sink.add(ExternalPrice(int(r.ts_ns), sym, float(r.binance_mid), "binance",
                                   float(r.binance_best_bid), float(r.binance_best_ask)))
        for ticker, g in k.groupby("kalshi_market_ticker"):
            close = crypto15m_close_from_ticker(ticker)
            series = ticker.split("-")[0]
            sink.add_market(dict(ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], series_ticker=series,
                                 title=f"{g['kalshi_asset'].iloc[0]} 15-minute up/down", status="settled",
                                 market_type="binary", close_ts=close,
                                 open_ts=None if close is None else close - 900,
                                 depth_quality="full", underlying=f"{g['kalshi_asset'].iloc[0]}-USD",
                                 source=source))
            res = str(g["kalshi_resolution"].dropna().iloc[0]).lower() if g["kalshi_resolution"].notna().any() else ""
            if res in ("yes", "no"):
                sink.add(Settlement(int((close or g["ts_ns"].max() / 1e9) * 1e9), ticker,
                                    1.0 if res == "yes" else 0.0, res, inferred=True))
        n = sink.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    tob = derive_tob(db, sorted(k["kalshi_market_ticker"].unique().tolist()))
    return {"events": n, "tob_rows": tob, "markets": int(k["kalshi_market_ticker"].nunique())}


def import_crypto_sample_csv(db: Database, path: str | Path, assumed_level_qty: float = 10.0) -> Dict[str, int]:
    """Import top-of-book samples. Level sizes are unknown and replaced by
    ``assumed_level_qty`` (recorded in ``markets.depth_quality``); settlement
    is inferred from the final quote at expiry and flagged as inferred."""
    df = pd.read_csv(path)
    df["ts_ns"] = (df["ts_unix"] * 1e9).astype("int64")
    source = "import:crypto_sample_csv"
    coins = sorted({c.split("_")[0] for c in df.columns if c.endswith("_ticker")})
    db.begin()
    try:
        sink = EventSink(db, source)
        per_market_last: Dict[str, dict] = {}
        rows = df.sort_values("ts_ns").to_dict("records")
        for r in rows:
            for c in coins:
                t = r.get(f"{c}_ticker")
                if not isinstance(t, str) or pd.isna(r.get(f"{c}_yes_bid")):
                    continue
                bid, ask = int(r[f"{c}_yes_bid"]), int(r[f"{c}_yes_ask"])
                per_market_last[t] = dict(coin=c, strike=r.get(f"{c}_floor_strike"), mid=(bid + ask) / 200.0,
                                          tte=r.get(f"{c}_time_to_expiry"), ts_ns=int(r["ts_ns"]),
                                          spot=r.get(f"{c}_real_price"))
                if not (0 < bid < ask < 100):
                    continue  # one-sided / pinned quotes are kept only for settlement inference
                q = float(assumed_level_qty)
                sink.add(BookSnapshot(int(r["ts_ns"]), t, [(bid * CENT, q)], [((100 - ask) * CENT, q)]))
                if not pd.isna(r.get(f"{c}_real_price")):
                    sink.add(ExternalPrice(int(r["ts_ns"]), f"{c.upper()}-USD", float(r[f"{c}_real_price"]),
                                           "crypto_sample"))
        for t, last in per_market_last.items():
            close = crypto15m_close_from_ticker(t)
            sink.add_market(dict(ticker=t, event_ticker=t.rsplit("-", 1)[0], series_ticker=t.split("-")[0],
                                 title=f"{last['coin'].upper()} 15-minute up/down", status="settled",
                                 market_type="binary", close_ts=close,
                                 open_ts=None if close is None else close - 900,
                                 strike_type="greater_or_equal",
                                 floor_strike=None if pd.isna(last["strike"]) else float(last["strike"]),
                                 depth_quality=f"top_only_assumed_{assumed_level_qty:g}",
                                 underlying=f"{last['coin'].upper()}-USD", source=source))
            # Infer settlement only when the market was observed at expiry and pinned.
            if last["tte"] is not None and not pd.isna(last["tte"]) and float(last["tte"]) <= 1.0:
                if last["mid"] >= 0.9 or last["mid"] <= 0.1:
                    res = "yes" if last["mid"] >= 0.9 else "no"
                    sink.add(Settlement(int((close or last["ts_ns"] / 1e9) * 1e9), t,
                                        1.0 if res == "yes" else 0.0, res, inferred=True))
        n = sink.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    tob = derive_tob(db, sorted(per_market_last))
    return {"events": n, "tob_rows": tob, "markets": len(per_market_last)}
