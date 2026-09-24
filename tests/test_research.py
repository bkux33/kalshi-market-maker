import json
from pathlib import Path

import pandas as pd
import pytest

from alphalab.core.config import ValidationConfig
from alphalab.data.synthetic import SyntheticSpec, write_synthetic
from alphalab.research import predictive
from alphalab.research import validation as V
from alphalab.research.experiments import ExperimentSpec, expand_grid, run_experiment
from alphalab.research.reports import load_experiments, research_report, scorecard

CFG = ValidationConfig()


def test_grid_cap_refuses_brute_force():
    with pytest.raises(ValueError, match="max_grid_size"):
        expand_grid({}, {"a": list(range(10)), "b": list(range(10))}, 64)
    assert len(expand_grid({"x": 1}, {"a": [1, 2], "b": [3]}, 64)) == 2


def test_classification_rules():
    assert V.classify({"n_trades": 0}, [], None, CFG)[0] == V.REJECT
    assert V.classify({"n_trades": 500, "net_pnl": -1}, [], None, CFG)[0] == V.REJECT
    assert V.classify({"n_trades": 10, "net_pnl": 5}, [], None, CFG)[0] == V.RESEARCH
    gating = [{"code": "fails_fees_x2", "gating": True, "message": ""}]
    assert V.classify({"n_trades": 500, "net_pnl": 5}, gating, {"net_pnl": 1}, CFG)[0] == V.RESEARCH
    assert V.classify({"n_trades": 500, "net_pnl": 5}, [], None, CFG)[1] == "holdout test not evaluated"
    assert V.classify({"n_trades": 500, "net_pnl": 5}, [], {"net_pnl": 1}, CFG)[0] == V.PAPER


def test_flags_detect_classic_overfitting_symptoms():
    oos = {"n_trades": 40, "net_pnl": 10, "gross_pnl": 12, "mid_pnl": 15, "expectancy": 0.05,
           "sharpe_per_trade": 1.5, "top_market_share": 0.9, "top_day_share": 0.95, "n_days": 1, "n_markets": 2}
    flags = V.compute_flags(oos=oos, is_metrics={"expectancy": 1.0}, test=None,
                            stress={"fees_x2": {"net_pnl": -3}}, sensitivity={"positive_neighbor_frac": 0.2},
                            wf_fold_nets=[5, -1, -2], pvalue=0.4, psr=0.5, n_params=9, data_quality={}, cfg=CFG,
                            benchmark_trials=500)
    codes = {f["code"] for f in flags}
    assert {"small_sample", "suspicious_sharpe", "concentrated_market", "concentrated_day", "few_days",
            "fails_fees_x2", "parameter_sensitive", "too_many_parameters", "is_oos_degradation",
            "walk_forward_inconsistent", "not_better_than_random", "low_deflated_sharpe"} <= codes
    dies = V.compute_flags(oos={"n_trades": 400, "net_pnl": -1, "gross_pnl": 3, "mid_pnl": 5}, is_metrics=None,
                           test=None, stress={}, sensitivity=None, wf_fold_nets=[], pvalue=None, psr=None,
                           n_params=1, data_quality={}, cfg=CFG)
    assert "dies_after_costs" in {f["code"] for f in dies}
    under = V.compute_flags(oos={"n_trades": 400, "net_pnl": 1}, is_metrics=None, test=None, stress={},
                            sensitivity=None, wf_fold_nets=[], pvalue=0.01, psr=None, n_params=1, data_quality={},
                            cfg=CFG, benchmark_trials=20)
    assert "benchmark_underpowered" in {f["code"] for f in under}


def test_psr_and_expected_max_sharpe():
    good = [0.1 + 0.01 * ((i * 7) % 5 - 2) for i in range(200)]
    assert V.probabilistic_sharpe(good, 0.0) > 0.99
    noise = [(-1) ** i * 0.1 for i in range(200)]
    assert V.probabilistic_sharpe(noise, 0.0) < 0.6
    assert V.expected_max_sharpe([0.0, 0.1, -0.1, 0.05, -0.05] * 10) > 0


def test_paper_promotion_rules():
    bt = {"expectancy": 0.10}
    assert V.evaluate_paper({"n_fills": 10, "n_days": 20, "net_pnl": 5, "expectancy": 0.1}, bt, CFG)[0] == V.PAPER
    assert V.evaluate_paper({"n_fills": 500, "n_days": 20, "net_pnl": -5, "expectancy": -0.1}, bt, CFG)[0] == V.RESEARCH
    assert V.evaluate_paper({"n_fills": 500, "n_days": 20, "net_pnl": 5, "expectancy": 0.01}, bt, CFG)[0] == V.PAPER
    assert V.evaluate_paper({"n_fills": 500, "n_days": 20, "net_pnl": 5, "expectancy": 0.09}, bt, CFG)[0] == V.LIVE_CANDIDATE


def test_null_data_is_rejected_end_to_end(db, tmp_path):
    write_synthetic(db, SyntheticSpec(n_markets=12, seed=21))
    rep = run_experiment(db, ExperimentSpec(name="null", strategy="imbalance",
                                            grid={"threshold": [0.6, 0.7]}, random_trials=10))
    assert rep.classification == V.REJECT
    assert rep.test_metrics is None  # holdout untouched when the candidate fails
    exp = load_experiments(db)[0]
    card = scorecard(exp)
    assert "Status: REJECT" in card and "Net P&L" in card and "not evidence" in card
    st = db.query_df("SELECT status FROM strategy_status")["status"].tolist()
    assert st == ["REJECT"]
    assert research_report(db, tmp_path).exists()
    from alphalab.research import analysis
    base = analysis.rerun_oos(db, rep.experiment_id)
    assert base["net_pnl"] == pytest.approx(rep.oos_metrics["net_pnl"])  # reproduces the walk-forward OOS path
    stressed = analysis.rerun_oos(db, rep.experiment_id, fee_stress=2.0, extra_latency_ms=100)
    assert stressed["fees"] >= base["fees"]
    assert rep.stress["fees_x2"]["fees"] == pytest.approx(2 * rep.oos_metrics["fees"], rel=1e-6)
    conc = analysis.concentration(db, rep.experiment_id)
    assert conc["n_trades"] == rep.oos_metrics["n_trades"]


def test_planted_edge_is_detected_but_not_promoted(db):
    write_synthetic(db, SyntheticSpec(n_markets=30, quote_lag_steps=60, seed=5))
    rep = run_experiment(db, ExperimentSpec(name="planted", strategy="crypto15m_fair_value",
                                            base_params={"series_prefix": "KXSYN", "cooldown_s": 30},
                                            grid={"edge_c": [4.0, 8.0]}, random_trials=10))
    assert rep.oos_metrics["net_pnl"] > 0 and rep.oos_metrics["mid_pnl"] > 0
    codes = {f["code"] for f in rep.flags}
    assert "synthetic_data" in codes and rep.classification in (V.RESEARCH,)


def test_feature_study_finds_planted_imbalance_signal(db):
    write_synthetic(db, SyntheticSpec(n_markets=8, imbalance_signal=0.9, lead_steps=20, seed=2))
    ds = predictive.build_dataset(db, horizons=(5, 10))
    assert {"fwd_5s", "fwd_10s", "fwd_settle", "imbalance"}.issubset(ds.columns)
    res = predictive.feature_study(db, ds, features=("imbalance", "ret_5s", "spread"), horizons=(5, 10))
    row = res[(res.feature == "imbalance") & (res.horizon_s == 10) & (res.split == "confirmation")].iloc[0]
    assert row.ic > 0.1 and row.ic_tstat > 2 and row.cost_hurdle > 0
    assert int(db.query_df("SELECT COUNT(*) n FROM predictive_results")["n"].iloc[0]) == len(res)


def test_incremental_information_needs_enough_markets(db):
    write_synthetic(db, SyntheticSpec(n_markets=3, seed=2))
    ds = predictive.build_dataset(db, horizons=(5,))
    out = predictive.incremental_information(ds, ["strike_z"])
    assert out["ok"] is False


def test_research_loop_end_to_end(db, settings, tmp_path):
    from alphalab.research.loop import run_research_loop
    write_synthetic(db, SyntheticSpec(n_markets=12, seed=31))
    out = run_research_loop(db, settings, strategies=["imbalance", "logical_arb"], out_dir=str(tmp_path),
                            random_trials=5)
    assert out["data_groups"] == {"synthetic": 12}
    assert out["experiments"]["imbalance@synthetic"]["classification"] in ("REJECT", "RESEARCH")
    assert "skipped" in out["experiments"]["logical_arb@synthetic"]
    assert out["paper_candidates"] == [] and Path(out["report"]).exists()
