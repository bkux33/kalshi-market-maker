"""Overfitting diagnostics and objective strategy classification.

Statuses (in increasing order of trust):

REJECT          out-of-sample net P&L <= 0, or the result disappears after fees.
RESEARCH        out-of-sample net P&L > 0 but at least one gating flag fires
                (sample too small, cost/latency stress fails, concentrated in
                one market/day, walk-forward inconsistent, parameter-sensitive,
                not better than random, low probabilistic Sharpe, ...).
PAPER           out-of-sample net P&L > 0, no gating flags, holdout test also
                positive. Eligible for paper trading - NOT for live trading.
LIVE-CANDIDATE  only assigned by ``evaluate_paper`` from paper-trading results
                that are large enough and consistent with the backtest. An
                experiment can never assign this status by itself.

Every threshold lives in ``ValidationConfig`` and is recorded with the result.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple

from alphalab.core.config import ValidationConfig

REJECT, RESEARCH, PAPER, LIVE_CANDIDATE = "REJECT", "RESEARCH", "PAPER", "LIVE-CANDIDATE"
STATUS_ORDER = {REJECT: 0, RESEARCH: 1, PAPER: 2, LIVE_CANDIDATE: 3}

_N = NormalDist()
_EULER = 0.5772156649


def _moments(x: Sequence[float]) -> Tuple[float, float, float, float]:
    n = len(x)
    mu = sum(x) / n
    var = sum((v - mu) ** 2 for v in x) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    if sd == 0:
        return mu, 0.0, 0.0, 3.0
    skew = sum(((v - mu) / sd) ** 3 for v in x) / n
    kurt = sum(((v - mu) / sd) ** 4 for v in x) / n
    return mu, sd, skew, kurt


def expected_max_sharpe(trial_sharpes: Sequence[float]) -> float:
    """Expected maximum Sharpe among N independent null trials (Bailey & Lopez de Prado)."""
    xs = [s for s in trial_sharpes if s is not None and math.isfinite(s)]
    n = len(xs)
    if n < 2:
        return 0.0
    mu = sum(xs) / n
    var = sum((s - mu) ** 2 for s in xs) / (n - 1)
    return math.sqrt(var) * ((1 - _EULER) * _N.inv_cdf(1 - 1.0 / n) + _EULER * _N.inv_cdf(1 - 1.0 / (n * math.e)))


def probabilistic_sharpe(returns: Sequence[float], benchmark_sr: float = 0.0) -> Optional[float]:
    """P(true per-trade Sharpe > benchmark_sr) given skew/kurtosis of the sample."""
    n = len(returns)
    if n < 3:
        return None
    mu, sd, skew, kurt = _moments(returns)
    if sd == 0:
        return None
    sr = mu / sd
    denom = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    if denom <= 0:
        return None
    return _N.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom))


def compute_flags(*, oos: Dict[str, Any], is_metrics: Optional[Dict[str, Any]], test: Optional[Dict[str, Any]],
                  stress: Dict[str, Dict[str, Any]], sensitivity: Optional[Dict[str, Any]],
                  wf_fold_nets: List[float], pvalue: Optional[float], psr: Optional[float],
                  n_params: int, data_quality: Dict[str, Any], cfg: ValidationConfig,
                  benchmark_trials: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return flags as {code, gating, message}. Gating flags block PAPER status."""
    f: List[Dict[str, Any]] = []

    def add(code: str, msg: str, gating: bool = True) -> None:
        f.append({"code": code, "gating": gating, "message": msg})

    n = oos.get("n_trades") or 0
    net = oos.get("net_pnl") or 0.0
    if n < cfg.min_trades_paper:
        add("small_sample", f"out-of-sample trades {n} < {cfg.min_trades_paper}")
    spt = oos.get("sharpe_per_trade")
    if spt is not None and n > 1 and (spt > 1.0 and n < 100 or spt * math.sqrt(n) > cfg.suspicious_sharpe * 3):
        add("suspicious_sharpe", f"per-trade Sharpe {spt:.2f} on {n} trades is implausibly high")
    tms = oos.get("top_market_share")
    if tms is not None and tms > cfg.max_single_market_pnl_share and net > 0:
        add("concentrated_market", f"{tms:.0%} of positive P&L from one market")
    tds = oos.get("top_day_share")
    if tds is not None and tds > cfg.max_single_day_pnl_share and net > 0:
        add("concentrated_day", f"{tds:.0%} of positive P&L from one day")
    if (oos.get("n_days") or 0) < cfg.min_distinct_days_paper:
        add("few_days", f"only {oos.get('n_days') or 0} distinct trading days out of sample")
    if (oos.get("n_markets") or 0) < cfg.min_distinct_markets_paper:
        add("few_markets", f"only {oos.get('n_markets') or 0} distinct markets out of sample")
    gross = oos.get("gross_pnl") or 0.0
    mid_pnl = oos.get("mid_pnl") or 0.0
    if (gross > 0 or mid_pnl > 0) and net <= 0:
        add("dies_after_costs", f"positive before costs (mid {mid_pnl:.2f}, gross {gross:.2f}) but net {net:.2f}")
    for name, m in stress.items():
        if m is None:
            continue
        if net > 0 and (m.get("net_pnl") or 0.0) <= 0:
            add(f"fails_{name}", f"net P&L {m.get('net_pnl', 0):.2f} under {name.replace('_', ' ')}")
    if sensitivity:
        frac = sensitivity.get("positive_neighbor_frac")
        if frac is not None and frac < 0.5:
            add("parameter_sensitive", f"only {frac:.0%} of neighbouring parameter sets are profitable")
    if n_params > cfg.max_param_count:
        add("too_many_parameters", f"{n_params} tuned parameters > {cfg.max_param_count}")
    if is_metrics and (is_metrics.get("expectancy") or 0) > 0:
        ise, oose = is_metrics["expectancy"], oos.get("expectancy") or 0.0
        if oose < cfg.max_is_oos_degradation * ise:
            add("is_oos_degradation", f"OOS expectancy {oose:.4f} < {cfg.max_is_oos_degradation:.0%} of IS {ise:.4f}")
    if wf_fold_nets:
        pos = sum(1 for x in wf_fold_nets if x > 0) / len(wf_fold_nets)
        if pos < cfg.min_wf_positive_frac:
            add("walk_forward_inconsistent", f"{pos:.0%} of walk-forward folds profitable")
    if pvalue is None:
        add("no_random_benchmark", "no random-entry benchmark available for this strategy type", gating=False)
    elif benchmark_trials is not None and 1.0 / (1 + benchmark_trials) > cfg.max_pvalue_vs_random / 2:
        add("benchmark_underpowered", f"{benchmark_trials} random trials cannot resolve p < "
            f"{cfg.max_pvalue_vs_random}; increase random_benchmark_trials")
    elif pvalue > cfg.max_pvalue_vs_random:
        add("not_better_than_random", f"p-value vs random-direction benchmark {pvalue:.3f}")
    if psr is not None and psr < 0.95:
        add("low_deflated_sharpe", f"probabilistic Sharpe vs multiple-testing benchmark {psr:.2f} < 0.95")
    if test is not None and (test.get("net_pnl") or 0.0) <= 0 and net > 0:
        add("fails_holdout", f"holdout test net P&L {test.get('net_pnl', 0):.2f}")
    if data_quality.get("inferred_settlements"):
        add("inferred_settlements", f"{data_quality['inferred_settlements']} settlements inferred, not official",
            gating=False)
    if data_quality.get("assumed_depth"):
        add("assumed_depth", "some markets have assumed (not recorded) depth", gating=True)
    if data_quality.get("synthetic"):
        add("synthetic_data", "results are on synthetic data", gating=True)
    return f


def classify(oos: Dict[str, Any], flags: List[Dict[str, Any]], test: Optional[Dict[str, Any]],
             cfg: ValidationConfig) -> Tuple[str, str]:
    n = oos.get("n_trades") or 0
    net = oos.get("net_pnl") or 0.0
    codes = {x["code"] for x in flags}
    if n == 0:
        return REJECT, "no out-of-sample trades"
    if net <= 0:
        return REJECT, f"out-of-sample net P&L {net:.2f} <= 0"
    if "dies_after_costs" in codes:
        return REJECT, "edge does not survive costs"
    if n < cfg.min_trades_research:
        return RESEARCH, f"positive but only {n} out-of-sample trades"
    gating = [x for x in flags if x["gating"]]
    if gating:
        return RESEARCH, "; ".join(x["code"] for x in gating)
    if test is None:
        return RESEARCH, "holdout test not evaluated"
    return PAPER, "all out-of-sample gates passed; eligible for paper trading only"


def evaluate_paper(paper: Dict[str, Any], backtest_oos: Dict[str, Any], cfg: ValidationConfig) -> Tuple[str, str]:
    """Promote PAPER -> LIVE-CANDIDATE only on sufficient, consistent paper evidence."""
    fills = paper.get("n_fills") or 0
    days = paper.get("n_days") or 0
    net = paper.get("net_pnl") or 0.0
    if fills < cfg.min_paper_fills_live:
        return PAPER, f"paper fills {fills} < {cfg.min_paper_fills_live}"
    if days < cfg.min_paper_days_live:
        return PAPER, f"paper days {days} < {cfg.min_paper_days_live}"
    if net <= 0:
        return RESEARCH, f"paper net P&L {net:.2f} <= 0 (demoted)"
    pe, be = paper.get("expectancy") or 0.0, backtest_oos.get("expectancy") or 0.0
    if be > 0 and pe < cfg.max_paper_backtest_gap * be:
        return PAPER, f"paper expectancy {pe:.4f} < {cfg.max_paper_backtest_gap:.0%} of backtest {be:.4f}"
    return LIVE_CANDIDATE, "paper results sufficient and consistent with backtest"


def config_dict(cfg: ValidationConfig) -> Dict[str, Any]:
    return asdict(cfg)
