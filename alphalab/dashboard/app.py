"""Dashboard: FastAPI JSON API + a single static page with client-side routes.

* Binds to 127.0.0.1 by default. Set ``DASHBOARD_TOKEN`` to require
  ``Authorization: Bearer <token>`` (or ``?token=``) on every API call.
* Reads DuckDB read-only and the paper/live state files; it never writes
  trading state except tripping the kill switch. Resetting the kill switch
  is deliberately CLI-only (``alphalab kill --reset``).
* Experiment tables show objective validation results; there is no "best
  strategy" ranking.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from alphalab.core.config import Settings
from alphalab.data.db import Database
from alphalab.execution.killswitch import KillSwitch
from alphalab.research.reports import DISCLAIMER, load_experiments, scorecard
from alphalab.strategies import REGISTRY

STATIC = Path(__file__).parent / "static"
PAGES = ("dashboard", "markets", "orderbook", "strategies", "experiments", "backtests", "paper", "risk", "trades",
         "performance", "settings")


def _clean(obj: Any) -> Any:
    if isinstance(obj, float):
        return None if obj != obj or obj in (float("inf"), float("-inf")) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Kalshi Alpha Lab", docs_url=None, redoc_url=None)
    token = os.environ.get("DASHBOARD_TOKEN")
    kill = KillSwitch(settings.risk.kill_switch_file)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if token and request.url.path.startswith("/api"):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
                request.query_params.get("token", "")
            if supplied != token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    def db() -> Database:
        if not settings.data.db_path.exists():
            raise HTTPException(404, "database not found - record/import data first")
        try:
            return Database(settings.data.db_path, read_only=True, lock_timeout_s=3)
        except Exception as exc:
            raise HTTPException(503, f"database busy ({type(exc).__name__}); retry shortly")

    def q(sql: str, params=None):
        d = db()
        try:
            return d.query_df(sql, params)
        finally:
            d.close()

    def state(mode: str) -> Optional[Dict[str, Any]]:
        p = settings.data.state_dir / f"{mode}_state.json"
        if not p.exists():
            return None
        try:
            s = json.loads(p.read_text())
            s["age_s"] = time.time() - s.get("updated", 0)
            return s
        except (json.JSONDecodeError, OSError):
            return None

    @app.get("/")
    def root():
        return FileResponse(STATIC / "index.html")

    for page in PAGES:
        app.add_api_route(f"/{page}", root, methods=["GET"], include_in_schema=False)

    @app.get("/api/overview")
    def overview():
        out: Dict[str, Any] = {"disclaimer": DISCLAIMER, "kill_switch": kill.info(), "trading_mode": settings.trading_mode,
                               "paper": None, "live": None}
        for mode in ("paper", "live"):
            s = state(mode)
            if s:
                out[mode] = {k: s.get(k) for k in ("run_id", "age_s", "pnl", "risk", "strategies")}
                out[mode]["n_positions"] = len(s.get("positions") or [])
                out[mode]["n_open_orders"] = len(s.get("open_orders") or [])
                out[mode]["metrics"] = {k: (s.get("metrics") or {}).get(k) for k in
                                        ("n_trades", "n_fills", "gross_pnl", "fees", "net_pnl", "max_drawdown")}
        try:
            out["data"] = q("""SELECT (SELECT COUNT(DISTINCT market) FROM book_events) markets,
                                      (SELECT COUNT(*) FROM book_events) book_events,
                                      (SELECT COUNT(*) FROM trades) trades,
                                      (SELECT COUNT(*) FROM experiments) experiments""").to_dict("records")[0]
            out["status_counts"] = q("SELECT classification, COUNT(*) n FROM experiments GROUP BY 1").to_dict("records")
        except HTTPException as exc:
            out["data_error"] = exc.detail
        return _clean(out)

    @app.get("/api/state/{mode}")
    def get_state(mode: str):
        if mode not in ("paper", "live"):
            raise HTTPException(404)
        return _clean(state(mode) or {"error": f"no {mode} session state"})

    @app.get("/api/markets")
    def markets():
        df = q("""WITH last AS (SELECT market, MAX(ts_ns) ts FROM tob GROUP BY market)
                  SELECT m.ticker, m.series_ticker, m.title, m.status, m.close_ts, m.result, m.depth_quality,
                         m.source, t.bid, t.ask, t.spread, t.mid, t.imbalance5, t.bid_depth5, t.ask_depth5, t.ts_ns
                  FROM markets m LEFT JOIN last l ON l.market = m.ticker
                  LEFT JOIN tob t ON t.market = l.market AND t.ts_ns = l.ts
                  ORDER BY m.close_ts DESC NULLS LAST LIMIT 1000""")
        return _clean(df.to_dict("records"))

    @app.get("/api/orderbook/{market}")
    def orderbook(market: str):
        for mode in ("live", "paper"):
            s = state(mode)
            if s and market in (s.get("books") or {}):
                return _clean({"source": f"{mode} session", **s["books"][market]})
        df = q("SELECT * FROM tob WHERE market = ? ORDER BY ts_ns DESC LIMIT 300", [market])
        if df.empty:
            raise HTTPException(404, "no data for market")
        return _clean({"source": "recorded top-of-book", "history": df.to_dict("records")})

    @app.get("/api/strategies")
    def strategies():
        reg = [{"name": n, "family": c.family, "tunable": list(c.tunable), "defaults": c.default_params()}
               for n, c in sorted(REGISTRY.items())]
        try:
            st = q("SELECT * FROM strategy_status ORDER BY updated_ns DESC").to_dict("records")
        except HTTPException:
            st = []
        return _clean({"registry": reg, "status": st})

    @app.get("/api/experiments")
    def experiments():
        d = db()
        try:
            exps = load_experiments(d)
        finally:
            d.close()
        rows = []
        for e in exps:
            s = e["summary"] or {}
            o, t = s.get("oos") or {}, s.get("test") or {}
            rows.append({"experiment_id": e["experiment_id"], "name": e["name"], "strategy": e["strategy"],
                         "params": e["selected_params"], "status": e["classification"], "reason": s.get("reason"),
                         "oos_trades": o.get("n_trades"), "oos_net": o.get("net_pnl"),
                         "oos_expectancy": o.get("expectancy"), "oos_sharpe_trade": o.get("sharpe_per_trade"),
                         "oos_max_dd": o.get("max_drawdown"), "oos_fees": o.get("fees"),
                         "is_expectancy": (s.get("is") or {}).get("expectancy"),
                         "holdout_net": t.get("net_pnl") if t else None, "grid_points": e.get("n_trials"),
                         "gating_flags": [f["code"] for f in e["flags"] or [] if f["gating"]],
                         "created_ns": e["created_ns"]})
        return _clean(rows)

    @app.get("/api/experiments/{exp_id}")
    def experiment(exp_id: str):
        d = db()
        try:
            exps = [e for e in load_experiments(d) if e["experiment_id"] == exp_id]
        finally:
            d.close()
        if not exps:
            raise HTTPException(404)
        return _clean({**exps[0], "scorecard": scorecard(exps[0])})

    @app.get("/api/runs")
    def runs(kind: Optional[str] = None, limit: int = 200):
        where, params = ("WHERE kind = ?", [kind]) if kind else ("", [])
        df = q(f"SELECT run_id, kind, strategy, params, phase, experiment_id, created_ns, metrics, result_hash "
               f"FROM runs {where} ORDER BY created_ns DESC LIMIT {int(limit)}", params)
        out = []
        for r in df.to_dict("records"):
            m = json.loads(r.pop("metrics") or "{}")
            r["params"] = json.loads(r["params"] or "{}")
            r.update({k: m.get(k) for k in ("n_trades", "gross_pnl", "fees", "net_pnl", "expectancy", "max_drawdown",
                                            "fill_rate", "adverse_selection_c")})
            out.append(r)
        return _clean(out)

    @app.get("/api/runs/{run_id}/pnl")
    def run_pnl(run_id: str):
        return _clean(q("SELECT ts_ns, equity, realized, unrealized, fees, gross_exposure FROM pnl WHERE run_id = ? "
                        "ORDER BY ts_ns", [run_id]).to_dict("records"))

    @app.get("/api/trades")
    def trades(run_id: Optional[str] = None, limit: int = 500):
        where, params = ("WHERE run_id = ?", [run_id]) if run_id else ("", [])
        return _clean(q(f"SELECT * FROM fills {where} ORDER BY ts_ns DESC LIMIT {int(limit)}", params)
                      .to_dict("records"))

    @app.get("/api/risk")
    def risk():
        try:
            ev = q("SELECT * FROM risk_events ORDER BY ts_ns DESC NULLS LAST LIMIT 300").to_dict("records")
        except HTTPException:
            ev = []
        live_state = {m: (state(m) or {}).get("risk") for m in ("paper", "live")}
        return _clean({"kill_switch": kill.info(), "kill_switch_file": str(kill.path), "sessions": live_state,
                       "limits": settings.risk.__dict__, "events": ev})

    @app.post("/api/kill")
    async def trip(request: Request):
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        kill.trip(str(body.get("reason") or "dashboard"), source="dashboard")
        return {"tripped": True, "info": kill.info(), "reset": "CLI only: alphalab kill --reset"}

    @app.get("/api/performance")
    def performance():
        df = q("""SELECT kind, strategy, COUNT(*) runs,
                         SUM(CAST(json_extract(metrics, '$.n_trades') AS DOUBLE)) trades,
                         SUM(CAST(json_extract(metrics, '$.net_pnl') AS DOUBLE)) net_pnl,
                         SUM(CAST(json_extract(metrics, '$.fees') AS DOUBLE)) fees
                  FROM runs GROUP BY 1, 2 ORDER BY 1, 2""")
        return _clean({"note": "Grouped by evidence type. 'experiment' rows are simulated; paper is simulated on "
                               "live data; only 'live' rows are real executions.",
                       "rows": df.to_dict("records")})

    @app.get("/api/predictive")
    def predictive():
        return _clean(q("""SELECT * FROM predictive_results WHERE study_id =
                           (SELECT study_id FROM predictive_results ORDER BY created_ns DESC LIMIT 1)
                           ORDER BY feature, horizon_s, split""").to_dict("records"))

    @app.get("/api/settings")
    def get_settings():
        return _clean(settings.redacted())

    return app
