"""Credential-free network pre-flight for the configured Kalshi endpoints (``alphalab net-check``).

Answers one question before anything else runs: can this machine reach the
configured REST and WebSocket hosts at all? For each host it reports DNS
resolution, the REST ``GET /exchange/status`` response and an unauthenticated
WebSocket handshake. A reachable Kalshi WebSocket rejects the unauthenticated
handshake itself (HTTP 401/403 from Kalshi) — that counts as *reachable*. A
rejection by an HTTP proxy (``CONNECT`` refused) is reported as *blocked by proxy*.
No credentials are read or sent.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Dict
from urllib.parse import urlparse

import httpx


def _dns(host: str) -> Dict[str, Any]:
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443)})
        return {"ok": True, "addresses": len(addrs)}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__}


def _classify(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "proxy" in text or "connect tunnel" in text:
        return "blocked_by_proxy"
    if "name or service" in text or "getaddrinfo" in text or "nodename" in text:
        return "dns_failure"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "connection_error"


def check_rest(rest_url: str, timeout: float = 15.0) -> Dict[str, Any]:
    host = urlparse(rest_url).hostname or ""
    out: Dict[str, Any] = {"url": rest_url.rstrip("/") + "/exchange/status", "host": host, "dns": _dns(host)}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(out["url"])
        out.update(reachable=True, http_status=r.status_code, result="reachable")
    except httpx.ProxyError as exc:
        out.update(reachable=False, result="blocked_by_proxy", error=str(exc)[:160])
    except httpx.HTTPError as exc:
        out.update(reachable=False, result=_classify(exc), error=str(exc)[:160])
    return out


async def _ws(ws_url: str, timeout: float) -> Dict[str, Any]:
    import websockets
    from websockets.exceptions import InvalidStatus
    host = urlparse(ws_url).hostname or ""
    out: Dict[str, Any] = {"url": ws_url, "host": host, "dns": _dns(host)}
    try:
        async with websockets.connect(ws_url, open_timeout=timeout):
            out.update(reachable=True, result="reachable", handshake="accepted_without_auth")
    except InvalidStatus as exc:  # the server itself answered (e.g. 401 without credentials)
        out.update(reachable=True, result="reachable", handshake=f"server_rejected_http_{exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001 - classify every failure mode
        out.update(reachable=False, result=_classify(exc), error=f"{type(exc).__name__}: {str(exc)[:140]}")
    return out


def check_ws(ws_url: str, timeout: float = 15.0) -> Dict[str, Any]:
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda l, ctx: None)  # silence transport teardown noise on proxy refusals
    try:
        return loop.run_until_complete(_ws(ws_url, timeout))
    finally:
        loop.close()


def run_netcheck(rest_url: str, ws_url: str) -> Dict[str, Any]:
    rest, ws = check_rest(rest_url), check_ws(ws_url)
    blocked = [x["host"] for x in (rest, ws) if not x["reachable"]]
    return {"rest": rest, "ws": ws, "ok": not blocked, "blocked_hosts": blocked}


def format_netcheck(r: Dict[str, Any]) -> str:
    lines = []
    for k in ("rest", "ws"):
        x = r[k]
        dns = "resolves" if x["dns"]["ok"] else f"fails ({x['dns'].get('error')})"
        detail = x.get("http_status") or x.get("handshake") or x.get("error", "")
        lines.append(f"{k.upper():4s} {x['host']:34s} DNS: {dns:9s} -> {x['result'].upper()} ({detail})")
    lines.append("NETWORK: " + ("PASS" if r["ok"] else f"BLOCKED: {', '.join(r['blocked_hosts'])}"))
    return "\n".join(lines)
