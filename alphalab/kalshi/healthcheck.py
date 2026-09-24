"""Market-data connectivity check (``alphalab demo-check``) — no orders are sent.

1. REST: public ``GET /exchange/status`` and authenticated ``GET /portfolio/balance``
   (only "ok/failed" is reported; no account values or credentials are printed).
2. Market selection via ``find_markets`` (nothing hard-coded) unless tickers are given.
3. Authenticated WebSocket: subscribe to ``orderbook_delta``, ``trade``, ``ticker`` and
   ``market_lifecycle_v2`` for the chosen markets for ``seconds``.
4. Rebuild each book from real snapshots/deltas, then cross-check the reconstructed top of
   book against a fresh REST ``GET /markets/{ticker}/orderbook``.
5. Optionally record every raw frame to the tape (so the check is also data collection).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

from alphalab.core.prices import PRICE_SCALE
from alphalab.kalshi.rest import KalshiAPIError
from alphalab.kalshi.ws import PUBLIC_CHANNELS, KalshiWebSocket


def _c(units: Optional[int]) -> Optional[float]:
    return None if units is None else units / 100.0


async def run_check(rest, signer, ws_url: str, markets: List[str], seconds: float, env_label: str,
                    tape=None, connect=None, check_auth: bool = True) -> Dict[str, Any]:
    rep: Dict[str, Any] = {"environment": env_label, "rest_url": rest.base_url, "ws_url": ws_url,
                           "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "markets": {}}
    try:
        st = rest.get_exchange_status()
        rep["rest_public"] = {"ok": True, "exchange_active": st.get("exchange_active"),
                              "trading_active": st.get("trading_active")}
    except KalshiAPIError as exc:
        rep["rest_public"] = {"ok": False, "error": f"HTTP {exc.status}"}
    if check_auth and signer is not None:
        try:
            rest.get_balance()
            rep["rest_auth"] = {"ok": True}
        except KalshiAPIError as exc:
            rep["rest_auth"] = {"ok": False, "error": f"HTTP {exc.status}"}
    per: Dict[str, Dict[str, Any]] = {m: {"messages": 0, "snapshots": 0, "deltas": 0, "trades": 0}
                                       for m in markets}
    statuses: List[str] = []

    def on_event(ev):
        if isinstance(ev, dict):
            return
        m = getattr(ev, "market", None)
        if m in per:
            per[m]["messages"] += 1
            key = {"snapshot": "snapshots", "delta": "deltas", "trade": "trades"}.get(ev.kind)
            if key:
                per[m][key] += 1

    kw = {"connect": connect} if connect else {}
    ws = KalshiWebSocket(ws_url, signer, markets, channels=PUBLIC_CHANNELS, on_event=on_event,
                         on_raw=(lambda raw, ns: tape.write("ws", raw, ns)) if tape else None,
                         on_status=lambda s, i: (statuses.append(s),
                                                 tape.write("note", {"status": s, **i}) if tape else None),
                         max_backoff_s=5.0, **kw)
    task = asyncio.create_task(ws.run())
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        await asyncio.sleep(0.25)
    synced_at_end = {m: ws.books.is_synced(m) for m in markets}   # before stop() clears sync state
    await ws.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    rep["ws"] = {"connected_ever": ws.stats["connections"] > 0, "connections": ws.stats["connections"],
                 "reconnects": ws.stats["reconnects"], "messages": ws.stats["messages"],
                 "by_type": dict(ws.stats["by_type"]), "sequence_gaps": ws.stats["gaps"],
                 "duplicates": ws.stats["duplicates"], "malformed": ws.stats["malformed"],
                 "error_frames": ws.stats["error_frames"], "statuses": statuses[-20:]}
    rep["book_engine"] = ws.books.stats()
    for m in markets:
        ob = ws.books.books.get(m)
        info = dict(per[m])
        info["synced"] = synced_at_end[m]
        if ob is not None:
            info.update(best_bid_c=_c(ob.bid_price), best_ask_c=_c(ob.ask_price), spread_c=_c(ob.spread()),
                        yes_levels=len(ob.yes), no_levels=len(ob.no))
            try:
                yes, no = rest.get_orderbook(m, depth=0)
                rb = max((p for p, _ in yes), default=None)
                ra = (PRICE_SCALE - max(p for p, _ in no)) if no else None
                info["rest_best_bid_c"], info["rest_best_ask_c"] = _c(rb), _c(ra)
                info["rest_crosscheck"] = "match" if (rb == ob.bid_price and ra == ob.ask_price) else "differs"
            except KalshiAPIError as exc:
                info["rest_crosscheck"] = f"rest error {exc.status}"
        rep["markets"][m] = info
    rep["ok"] = bool(rep["ws"]["connected_ever"] and rep["ws"]["messages"] > 0
                     and any(v.get("snapshots", 0) > 0 for v in rep["markets"].values()))
    return rep


def format_report(rep: Dict[str, Any]) -> str:
    ws = rep.get("ws", {})
    lines = ["CONNECTED" if ws.get("connected_ever") else "NOT CONNECTED",
             f"Environment: {rep['environment']}",
             f"REST public: {'ok' if rep.get('rest_public', {}).get('ok') else rep.get('rest_public')}",
             f"REST auth:   {'ok' if rep.get('rest_auth', {}).get('ok') else rep.get('rest_auth', 'not checked')}",
             f"WebSocket messages received: {ws.get('messages', 0)}  by type: {ws.get('by_type', {})}",
             f"Sequence gaps: {ws.get('sequence_gaps', 0)}  Duplicates: {ws.get('duplicates', 0)}  "
             f"Malformed: {ws.get('malformed', 0)}  Reconnects: {ws.get('reconnects', 0)}"]
    for m, i in rep.get("markets", {}).items():
        lines += [f"Market: {m}",
                  f"  Messages: {i.get('messages')}  snapshots: {i.get('snapshots')}  deltas: {i.get('deltas')}  "
                  f"trades: {i.get('trades')}  synced: {i.get('synced')}",
                  f"  Best bid: {i.get('best_bid_c')}c  Best ask: {i.get('best_ask_c')}c  Spread: {i.get('spread_c')}c  "
                  f"Book levels: {i.get('yes_levels')} yes / {i.get('no_levels')} no",
                  f"  REST cross-check: {i.get('rest_crosscheck', 'n/a')} (REST {i.get('rest_best_bid_c')}/"
                  f"{i.get('rest_best_ask_c')})"]
    lines.append("RESULT: " + ("OK" if rep.get("ok") else "FAILED"))
    return "\n".join(lines)
