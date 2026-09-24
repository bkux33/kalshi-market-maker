"""Command-line interface: ``alphalab <command>`` (or ``python -m alphalab``)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from alphalab.core.config import Settings, load_settings
from alphalab.core.logs import setup_logging

log = logging.getLogger("alphalab")


def _json(s: Optional[str]) -> Dict[str, Any]:
    return json.loads(s) if s else {}


def _db(settings: Settings, read_only: bool = False):
    from alphalab.data.db import Database
    return Database(settings.data.db_path, read_only=read_only)


def _rest(settings: Settings, auth: bool = False):
    from alphalab.kalshi.auth import KalshiSigner
    from alphalab.kalshi.rest import KalshiREST
    signer = None
    if settings.kalshi.has_credentials:
        signer = KalshiSigner.from_file(settings.kalshi.api_key_id, settings.kalshi.private_key_path)
    elif auth:
        sys.exit("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are required for this command")
    return KalshiREST(settings.kalshi.rest_base, signer, settings.kalshi.requests_per_second,
                      settings.kalshi.timeout_s), signer


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------- commands
def cmd_discover(a, s: Settings) -> None:
    from alphalab.kalshi.discovery import discover, rank_for_recording
    rest, _ = _rest(s)
    ms = rank_for_recording(discover(rest, a.series or s.series), min_seconds_to_close=0)[: a.limit]
    for m in ms:
        print(f"{m.ticker:40s} bid={m.yes_bid} ask={m.yes_ask} vol24h={m.volume_24h} close={m.close_ts}")


def cmd_record(a, s: Settings) -> None:
    from alphalab.data.recorder import run_recorder
    rest, signer = _rest(s, auth=True)
    asyncio.run(run_recorder(s, rest, signer, a.series or s.series, a.markets or s.markets, external=a.external,
                             max_markets=a.max_markets))


def cmd_ingest(a, s: Settings) -> None:
    from alphalab.data.ingest import ingest_tape
    from alphalab.execution.journal import ingest_journals
    with _db(s) as db:
        _print({"tape": ingest_tape(db, s.data.raw_dir, include_open=a.include_open),
                "journals": ingest_journals(db, s.data.journal_dir, include_active=a.include_open)})


def cmd_import(a, s: Settings) -> None:
    from alphalab.data.importers import import_baseline_csv, import_crypto_sample_csv
    with _db(s) as db:
        fn = import_baseline_csv if a.format == "baseline" else import_crypto_sample_csv
        _print(fn(db, a.path))


def cmd_synthetic(a, s: Settings) -> None:
    from alphalab.data.synthetic import SyntheticSpec, write_synthetic
    spec = SyntheticSpec(n_markets=a.markets, imbalance_signal=a.imbalance_signal, quote_lag_steps=a.lag_steps,
                         seed=a.seed, series=a.series)
    with _db(s) as db:
        _print(write_synthetic(db, spec))


def cmd_backtest(a, s: Settings) -> None:
    from alphalab.sim.backtest import BacktestConfig, run_backtest
    from alphalab.sim.replay import ReplaySpec
    from alphalab.strategies import get_strategy
    cls = get_strategy(a.strategy)
    params = _json(a.params)
    cfg = BacktestConfig(replay=ReplaySpec(markets=a.markets or None), fill=s.fill, fees=s.fees,
                         fee_stress=a.fee_stress, record_signals=True)
    with _db(s) as db:
        r = run_backtest(db, lambda: cls(**params), cfg)
    keys = ["n_trades", "n_fills", "mid_pnl", "gross_pnl", "fees", "est_slippage", "net_pnl", "expectancy",
            "win_rate", "median_trade", "profit_factor", "sharpe_per_trade", "max_drawdown", "avg_exposure",
            "turnover", "avg_holding_s", "fill_rate", "adverse_selection_c", "avg_abs_inventory",
            "max_abs_inventory", "n_markets", "n_days"]
    print(f"run_id={r.run_id} result_hash={r.result_hash} data_hash={r.data_hash} ({r.elapsed_s:.1f}s)")
    print("HISTORICAL SIMULATION ONLY - in-sample, single run, no validation. Not evidence of an edge.")
    _print({k: r.metrics.get(k) for k in keys})


def cmd_experiment(a, s: Settings) -> None:
    from alphalab.research.experiments import ExperimentSpec, run_experiment
    from alphalab.research.reports import load_experiments, scorecard
    spec = ExperimentSpec(name=a.name or f"{a.strategy}", strategy=a.strategy, base_params=_json(a.base),
                          grid=_json(a.grid), markets=a.markets or None, series=a.series or None,
                          split_by=a.split_by, fill=s.fill, fees=s.fees, validation=s.validation,
                          random_trials=a.random_trials)
    with _db(s) as db:
        rep = run_experiment(db, spec, progress=print)
        exp = next(e for e in load_experiments(db) if e["experiment_id"] == rep.experiment_id)
        print(scorecard(exp))


def cmd_features(a, s: Settings) -> None:
    from alphalab.research import predictive
    with _db(s) as db:
        ds = predictive.build_dataset(db, sample_every_s=a.sample_every)
        res = predictive.feature_study(db, ds)
        if res.empty:
            print("no data")
            return
        cols = ["feature", "horizon_s", "split", "n", "ic", "ic_tstat", "hit_rate", "cost_hurdle",
                "net_edge_top", "net_edge_bottom", "survives"]
        print(res[[c for c in cols if c in res.columns]].round(4).to_string(index=False))
        if {"strike_z", "fwd_settle"}.issubset(ds.columns):
            _print({"incremental_information(strike_z)": predictive.incremental_information(ds, ["strike_z"])})


def cmd_loop(a, s: Settings) -> None:
    from alphalab.research.loop import run_research_loop
    with _db(s) as db:
        _print(run_research_loop(db, s, strategies=a.strategies or None, out_dir=a.out,
                                 random_trials=a.random_trials, progress=print))


def cmd_scorecard(a, s: Settings) -> None:
    from alphalab.research.reports import load_experiments, scorecard
    with _db(s, read_only=True) as db:
        exps = load_experiments(db)
    for e in exps:
        if a.experiment_id in (None, e["experiment_id"]):
            print(scorecard(e))
            print("=" * 100)


def cmd_report(a, s: Settings) -> None:
    from alphalab.research.reports import research_report
    with _db(s) as db:
        print(research_report(db, a.out))


def _strategies_from_args(a) -> list:
    from alphalab.strategies import get_strategy
    specs = a.strategy if isinstance(a.strategy, list) else [a.strategy]
    params = _json(a.params)
    out = []
    for name in specs:
        cls = get_strategy(name)
        out.append(cls(**params.get(name, params if len(specs) == 1 else {})))
    return out


def cmd_paper(a, s: Settings) -> None:
    from alphalab.execution.session import TradingSession, run_live_feed, run_replay_feed
    strategies = _strategies_from_args(a)
    if a.replay:
        from alphalab.sim.replay import ReplaySource, ReplaySpec
        with _db(s, read_only=True) as db:
            src = ReplaySource(db, ReplaySpec(markets=a.markets or None)).materialize()
        sess = TradingSession(s, strategies, mode="paper", market_meta=src.meta)
        m = run_replay_feed(sess, src)
    else:
        from alphalab.data.tape import TapeWriter
        rest, signer = _rest(s, auth=True)  # Kalshi WebSocket requires authentication even for market data
        sess = TradingSession(s, strategies, mode="paper")
        tape = TapeWriter(s.data.raw_dir, prefix="paper") if a.record else None
        m = asyncio.run(run_live_feed(sess, rest, s.kalshi.ws_base, signer, a.series or [], a.markets or [],
                                      record_tape=tape))
    print(f"paper session {sess.run_id} finished. Journal: {s.data.journal_dir}. PAPER RESULTS ARE SIMULATED.")
    _print({k: m.get(k) for k in ("n_trades", "n_fills", "gross_pnl", "fees", "net_pnl", "expectancy",
                                  "fill_rate", "adverse_selection_c")})


def cmd_paper_evaluate(a, s: Settings) -> None:
    from alphalab.research.experiments import strategy_key
    from alphalab.research.validation import evaluate_paper
    from alphalab.execution.journal import ingest_journals
    from alphalab.sim.metrics import compute_metrics
    from types import SimpleNamespace
    import time as _t
    with _db(s) as db:
        ingest_journals(db, s.data.journal_dir)
        st = db.query_df("SELECT * FROM strategy_status WHERE strategy_key = ?", [a.strategy_key])
        if st.empty:
            sys.exit(f"unknown strategy key {a.strategy_key}")
        row = st.iloc[0]
        if row["status"] not in ("PAPER", "LIVE-CANDIDATE"):
            sys.exit(f"strategy status is {row['status']}; only PAPER strategies can be evaluated for promotion")
        name, params = row["strategy"], json.loads(row["params"])
        runs = db.query_df("SELECT run_id, params FROM runs WHERE kind = 'paper'")
        run_ids = [r.run_id for r in runs.itertuples() if name in json.loads(r.params or "{}")
                   and json.loads(r.params)[name] == params]
        if not run_ids:
            sys.exit("no paper sessions found for this exact strategy/parameter set")
        ph = ",".join("?" for _ in run_ids)
        fills = db.query_df(f"SELECT * FROM fills WHERE run_id IN ({ph}) AND strategy = ?", run_ids + [name])
        metrics = [json.loads(m) for m in db.query_df(f"SELECT metrics FROM runs WHERE run_id IN ({ph})", run_ids)["metrics"]]
        net = sum((m or {}).get("net_pnl") or 0.0 for m in metrics)
        trades = sum((m or {}).get("n_trades") or 0 for m in metrics)
        days = fills["ts_ns"].map(lambda t: _t.strftime("%Y-%m-%d", _t.gmtime(t / 1e9))).nunique() if not fills.empty else 0
        paper = {"n_fills": int(len(fills)), "n_days": int(days), "net_pnl": net,
                 "expectancy": (net / trades) if trades else 0.0, "n_trades": trades}
        exp = db.query_df("SELECT summary FROM experiments WHERE experiment_id = ?", [row["experiment_id"]])
        bt = json.loads(exp["summary"].iloc[0]).get("oos") or {} if not exp.empty else {}
        status, reason = evaluate_paper(paper, bt, s.validation)
        db.execute("UPDATE strategy_status SET status = ?, reason = ?, updated_ns = ? WHERE strategy_key = ?",
                   [status, reason, _t.time_ns(), a.strategy_key])
        _print({"paper": paper, "backtest_oos_expectancy": bt.get("expectancy"), "status": status, "reason": reason})


def cmd_live(a, s: Settings) -> None:
    from alphalab.execution.live import LiveBroker, LiveTradingRefused, assert_live_allowed
    from alphalab.execution.session import TradingSession, run_live_feed
    from alphalab.research.experiments import strategy_key
    strategies = _strategies_from_args(a)
    keys = [strategy_key(st.name, st.params) for st in strategies]
    try:
        if s.data.db_path.exists():
            with _db(s, read_only=True) as db:
                assert_live_allowed(s, db, keys, demo_integration=a.demo_integration)
        else:
            assert_live_allowed(s, None, keys, demo_integration=a.demo_integration)
    except LiveTradingRefused as exc:
        sys.exit(str(exc))
    rest, signer = _rest(s, auth=True)
    sess = TradingSession(s, strategies, mode="live",
                          broker_factory=lambda books, fees, pf, close: LiveBroker(rest, books, fees, pf, close))
    m = asyncio.run(run_live_feed(sess, rest, s.kalshi.ws_base, signer, a.series or [], a.markets or [],
                                  private_channels=True))
    _print(m)


def cmd_kill(a, s: Settings) -> None:
    from alphalab.execution.killswitch import KillSwitch
    ks = KillSwitch(s.risk.kill_switch_file)
    if a.reset:
        print("kill switch reset" if ks.reset() else "kill switch was not tripped")
    elif a.status:
        print(json.dumps(ks.info()) if ks.is_tripped(force=True) else "not tripped")
    else:
        ks.trip(a.reason or "manual", source="cli")
        print(f"KILL SWITCH TRIPPED ({s.risk.kill_switch_file}). Running sessions cancel all orders and halt.")


def cmd_dashboard(a, s: Settings) -> None:
    import uvicorn
    from alphalab.dashboard.app import create_app
    uvicorn.run(create_app(s), host=a.host, port=a.port, log_level="warning")


def cmd_ask(a, s: Settings) -> None:
    from alphalab.research import assistant
    with _db(s, read_only=True) as db:
        if s.llm.enabled:
            print(assistant.answer(db, a.question, model=s.llm.model))
        else:
            _print(assistant.answer_offline(db, a.question, a.experiment_id))


def cmd_db_check(a, s: Settings) -> None:
    with _db(s, read_only=True) as db:
        _print(db.integrity_report())


def cmd_config(a, s: Settings) -> None:
    _print(s.redacted())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alphalab", description="Kalshi Alpha Lab - research-first Kalshi trading lab")
    p.add_argument("--config", help="YAML config file (default: $ALPHALAB_CONFIG)")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("discover", cmd_discover, "list open markets for series (public REST)")
    sp.add_argument("--series", nargs="*")
    sp.add_argument("--limit", type=int, default=50)
    sp = add("record", cmd_record, "record raw market data to the tape (needs API key for WebSocket)")
    sp.add_argument("--series", nargs="*")
    sp.add_argument("--markets", nargs="*")
    sp.add_argument("--external", action="store_true", help="also record Coinbase spot prices")
    sp.add_argument("--max-markets", type=int, default=100)
    sp = add("ingest", cmd_ingest, "load closed tape files and journals into DuckDB")
    sp.add_argument("--include-open", action="store_true")
    sp = add("import-csv", cmd_import, "import a third-party CSV sample")
    sp.add_argument("format", choices=["baseline", "crypto_sample"])
    sp.add_argument("path")
    sp = add("synthetic", cmd_synthetic, "generate synthetic markets (pipeline validation only)")
    sp.add_argument("--markets", type=int, default=20)
    sp.add_argument("--imbalance-signal", type=float, default=0.0)
    sp.add_argument("--lag-steps", type=int, default=0)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--series", default="KXSYN15M")
    sp = add("backtest", cmd_backtest, "single backtest run (diagnostic; not validation)")
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--params")
    sp.add_argument("--markets", nargs="*")
    sp.add_argument("--fee-stress", type=float, default=1.0)
    sp = add("experiment", cmd_experiment, "bounded grid + walk-forward + stress + classification")
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--grid", help='JSON, e.g. {"threshold":[0.6,0.7]}')
    sp.add_argument("--base", help="JSON base params")
    sp.add_argument("--name")
    sp.add_argument("--markets", nargs="*")
    sp.add_argument("--series", nargs="*")
    sp.add_argument("--split-by", default="auto", choices=["auto", "market", "time"])
    sp.add_argument("--random-trials", type=int)
    sp = add("features", cmd_features, "predictive-feature study (IC, cost hurdle, discovery/confirmation)")
    sp.add_argument("--sample-every", type=float, default=1.0)
    sp = add("research-loop", cmd_loop, "full automated research loop + report")
    sp.add_argument("--strategies", nargs="*")
    sp.add_argument("--out", default="reports")
    sp.add_argument("--random-trials", type=int)
    sp = add("scorecard", cmd_scorecard, "print experiment scorecards")
    sp.add_argument("experiment_id", nargs="?")
    sp = add("report", cmd_report, "write a markdown research report")
    sp.add_argument("--out", default="reports")
    for name, fn, help_ in (("paper", cmd_paper, "paper trading (simulated fills, never real orders)"),
                            ("live", cmd_live, "LIVE trading - refused unless every gate passes")):
        sp = add(name, fn, help_)
        sp.add_argument("--strategy", nargs="+", required=True)
        sp.add_argument("--params", help='JSON params (or {"strategy_name": {...}} for several)')
        sp.add_argument("--series", nargs="*")
        sp.add_argument("--markets", nargs="*")
        if name == "paper":
            sp.add_argument("--replay", action="store_true", help="drive paper trading from recorded data")
            sp.add_argument("--record", action="store_true", help="also record the raw feed to the tape")
        else:
            sp.add_argument("--demo-integration", action="store_true",
                            help="allow unvalidated strategies against the DEMO exchange only")
    sp = add("paper-evaluate", cmd_paper_evaluate, "promote PAPER -> LIVE-CANDIDATE if paper evidence suffices")
    sp.add_argument("strategy_key")
    sp = add("kill", cmd_kill, "trip / reset / inspect the kill switch")
    sp.add_argument("--reset", action="store_true")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--reason")
    sp = add("dashboard", cmd_dashboard, "web dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp = add("ask", cmd_ask, "research assistant (LLM if enabled, else deterministic analyses)")
    sp.add_argument("question")
    sp.add_argument("--experiment-id")
    add("db-check", cmd_db_check, "database integrity report")
    add("config", cmd_config, "print effective configuration (secrets redacted)")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    settings = load_settings(args.config)
    from alphalab.kalshi.rest import KalshiAPIError
    try:
        args.fn(args, settings)
    except KalshiAPIError as exc:
        sys.exit(f"Kalshi API error: {exc}. Check network access, KALSHI_ENV and credentials.")


if __name__ == "__main__":
    main()
