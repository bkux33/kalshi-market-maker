"""Market-data recorder service.

Captures everything needed for faithful replay to the append-only tape:
raw WebSocket frames (order-book snapshots/deltas, trades, ticker, lifecycle),
REST market metadata (on discovery and when results post), series fee
metadata, and optionally external reference prices (Coinbase public ticker).

Run: ``alphalab record --series KXBTC15M KXETH15M --external``
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any, Dict, Iterable, List, Optional

from alphalab.core.config import Settings
from alphalab.data.tape import TapeWriter
from alphalab.kalshi.discovery import discover, find_markets, rank_for_recording
from alphalab.kalshi.ws import PUBLIC_CHANNELS, KalshiWebSocket

log = logging.getLogger(__name__)

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
from alphalab.data.ingest import SERIES_UNDERLYING


async def coinbase_feed(tape: TapeWriter, products: List[str], stop: asyncio.Event) -> None:
    import websockets
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(COINBASE_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": products, "channels": ["ticker"]}))
                backoff = 1.0
                async for raw in ws:
                    ns = time.time_ns()
                    m = json.loads(raw)
                    if m.get("type") == "ticker" and m.get("price"):
                        tape.write("ext", {"symbol": m["product_id"], "price": float(m["price"]),
                                           "bid": float(m["best_bid"]) if m.get("best_bid") else None,
                                           "ask": float(m["best_ask"]) if m.get("best_ask") else None,
                                           "source": "coinbase", "exch_time": m.get("time")}, ns)
                    if stop.is_set():
                        break
        except Exception as exc:
            log.warning("coinbase_feed_error", extra={"fields": {"error": str(exc)[:200]}})
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)


async def run_recorder(settings: Settings, rest, signer, series: Iterable[str], markets: Iterable[str] = (),
                       external: bool = False, max_markets: int = 100, rediscover_s: float = 60.0,
                       search: Optional[str] = None, category: Optional[str] = None,
                       duration_s: Optional[float] = None) -> Dict[str, Any]:
    series = list(series)
    from alphalab.data.tape import recover_open_files
    recovered = recover_open_files(settings.data.raw_dir, older_than_s=30.0)
    if recovered:
        log.warning("recorder_recovered_files", extra={"fields": {"files": [str(p) for p in recovered]}})
    tape = TapeWriter(settings.data.raw_dir)
    tape.write("note", {"status": "recorder_start", "recovered_files": len(recovered),
                        "rest_url": settings.kalshi.rest_base, "ws_url": settings.kalshi.ws_base,
                        "env": settings.kalshi.env})
    known: Dict[str, dict] = {}

    def pick() -> List[str]:
        chosen = list(markets)
        if series:
            found = rank_for_recording(discover(rest, series), min_seconds_to_close=10)[:max_markets]
        elif search or category or not chosen:
            found = find_markets(rest, status="open", search=search, category=category, two_sided=True,
                                 min_seconds_to_close=60)[:max_markets]
        else:
            found = []
        for mm in found:
            if mm.ticker not in known:
                raw = rest.get_market(mm.ticker)
                tape.write("market", raw)
                known[mm.ticker] = raw
        for s in series:
            if f"series:{s}" not in known:
                try:
                    tape.write("series", rest.get_series(s))
                    known[f"series:{s}"] = {}
                except Exception:
                    pass
        return list(dict.fromkeys(chosen + [m.ticker for m in found]))

    def poll_results() -> None:
        now = time.time()
        for t, raw in list(known.items()):
            if t.startswith("series:") or raw.get("_final"):
                continue
            close = raw.get("close_time")
            try:
                from datetime import datetime
                close_ts = datetime.fromisoformat(str(close).replace("Z", "+00:00")).timestamp() if close else None
            except ValueError:
                close_ts = None
            if close_ts and now > close_ts + 5:
                m = rest.get_market(t)
                if (m.get("result") or "") in ("yes", "no") or m.get("status") in ("settled", "finalized"):
                    tape.write("market", m)
                    m["_final"] = True
                    known[t] = m

    initial = await asyncio.to_thread(pick)
    log.info("recorder_start", extra={"fields": {"markets": len(initial), "series": series}})
    ws = KalshiWebSocket(settings.kalshi.ws_base, signer, initial, channels=PUBLIC_CHANNELS,
                         on_raw=lambda raw, ns: tape.write("ws", raw, ns),
                         on_status=lambda st, info: tape.write("note", {"status": st, **info}))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    async def maintenance():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=rediscover_s)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                new = await asyncio.to_thread(pick)
                await ws.add_markets(new)
                await asyncio.to_thread(poll_results)
            except Exception as exc:
                log.warning("recorder_maintenance_error", extra={"fields": {"error": str(exc)[:200]}})

    tasks = [asyncio.create_task(ws.run()), asyncio.create_task(maintenance())]
    if external:
        prods = sorted({SERIES_UNDERLYING[s] for s in series if s in SERIES_UNDERLYING}) or ["BTC-USD"]
        tasks.append(asyncio.create_task(coinbase_feed(tape, prods, stop)))
    if duration_s:
        loop.call_later(duration_s, stop.set)
    await stop.wait()
    await ws.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    tape.write("note", {"status": "recorder_stop", "stats": {k: v for k, v in ws.stats.items() if k != "by_type"}})
    tape.close()
    log.info("recorder_stop", extra={"fields": {"lines": tape.lines, "stats": ws.stats}})
    return {"lines": tape.lines, "markets": len(ws.markets), **{k: ws.stats[k] for k in
            ("messages", "reconnects", "gaps", "duplicates", "malformed", "connections")}}
