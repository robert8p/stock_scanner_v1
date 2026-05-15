from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pandas as pd

from .config import load_settings
from .db import (
    deserialize_replay_run,
    get_latest_replay_run,
    get_replay_run,
    init_db,
    list_replay_runs,
    upsert_replay_run,
)
from .providers import get_provider
from .scoring import SECTOR_ETF_MAP, compute_timing_metrics, normalize_sector_name, score_timing
from .storage import ensure_dir, sanitize_row, utc_now_iso, write_csv, write_json, write_text, zip_directory
from .universe import load_universe


REPLAY_STATUS_LOCK = threading.Lock()
REPLAY_STATUS: Dict[str, Any] = {
    "is_running": False,
    "replay_id": None,
    "phase": "idle",
    "message": "Ready",
    "progress_current": 0,
    "progress_total": 0,
    "updated_at": utc_now_iso(),
}
LAST_REPLAY_STARTED_AT = 0.0
LAST_REPLAY_IDEMPOTENCY_KEY: Optional[str] = None
REPLAY_COOLDOWN_SECONDS = 5
REQUIRED_REPLAY_ARTIFACTS = {
    "replay_summary.json",
    "score_band_metrics.csv",
    "calibration_table.csv",
    "candidate_outcomes.csv",
    "top_vs_rest_comparison.csv",
    "quantile_lift_table.csv",
    "regime_slice_metrics.csv",
    "discrimination_report.json",
    "monotonicity_diagnostics.json",
    "validation_log.txt",
    "config_used.json",
    "replay_artifact_manifest.json",
    "replay_parity_assessment.json",
    "surface_feature_report.json",
    "elite_policy_leaderboard.csv",
    "elite_policy_report.json",
}


class ReplayAlreadyRunningError(RuntimeError):
    pass


class ReplayCooldownError(RuntimeError):
    pass


def _update_replay_status(**kwargs: Any) -> None:
    with REPLAY_STATUS_LOCK:
        REPLAY_STATUS.update(kwargs)
        REPLAY_STATUS["updated_at"] = utc_now_iso()



def get_replay_status() -> Dict[str, Any]:
    init_db()
    settings = load_settings()
    with REPLAY_STATUS_LOCK:
        status = dict(REPLAY_STATUS)
    status["build"] = {
        "app_version": settings.app_version,
        "build_id": settings.build_id,
        "build_timestamp_utc": settings.build_timestamp_utc,
        "artifact_schema_version": settings.artifact_schema_version,
    }
    latest = deserialize_replay_run(get_latest_replay_run())
    if latest:
        status["latest_replay"] = latest
    return status



def _build_replay_record(replay_id: str, settings, status: str, started_at: str, message: str = "", **extra: Any) -> Dict[str, Any]:
    return {
        "replay_id": replay_id,
        "started_at": started_at,
        "ended_at": extra.get("ended_at"),
        "status": status,
        "progress_current": extra.get("progress_current", 0),
        "progress_total": extra.get("progress_total", 0),
        "phase": extra.get("phase", ""),
        "message": message,
        "provider": "demo" if settings.demo_mode else settings.default_provider,
        "replay_mode": extra.get("replay_mode", settings.replay_default_mode),
        "settings_json": json.dumps({
            "replay_ticker_limit": settings.replay_ticker_limit,
            "replay_max_snapshots": settings.replay_max_snapshots,
            "replay_history_days": settings.replay_history_days,
            "replay_warmup_days": settings.replay_warmup_days,
            "replay_step_days": settings.replay_step_days,
            "replay_min_rows_per_snapshot": settings.replay_min_rows_per_snapshot,
            "replay_default_mode": settings.replay_default_mode,
            "outcome_target_up_pct": settings.outcome_target_up_pct,
            "outcome_stop_down_pct": settings.outcome_stop_down_pct,
            "outcome_horizon_days": settings.outcome_horizon_days,
            "replay_monotonicity_min_correlation": settings.replay_monotonicity_min_correlation,
            "replay_min_top_decile_lift": settings.replay_min_top_decile_lift,
            "replay_min_top_quintile_lift": settings.replay_min_top_quintile_lift,
            "replay_max_monotonicity_violations": settings.replay_max_monotonicity_violations,
            "replay_monotonicity_tolerance": settings.replay_monotonicity_tolerance,
            "replay_policy_min_observations": settings.replay_policy_min_observations,
            "replay_policy_min_snapshots": settings.replay_policy_min_snapshots,
            "replay_policy_min_lift_vs_all": settings.replay_policy_min_lift_vs_all,
            "replay_policy_min_avg_end_return_pct": settings.replay_policy_min_avg_end_return_pct,
            "replay_policy_max_stop_rate": settings.replay_policy_max_stop_rate,
        }, default=str),
        "warnings_json": json.dumps(extra.get("warnings", []), default=str),
        "artifacts_dir": extra.get("artifacts_dir", ""),
        "artifact_zip_path": extra.get("artifact_zip_path", ""),
        "summary_json": json.dumps(extra.get("summary", {}), default=str),
    }



def _normalize_ticker(symbol: str) -> str:
    return symbol.replace(".", "-").upper()



def _safe_round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), digits)



def _pearson_corr(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or len(ys) < 3 or len(xs) != len(ys):
        return None
    sx = pd.Series(xs, dtype=float)
    sy = pd.Series(ys, dtype=float)
    corr = sx.corr(sy)
    if corr is None or pd.isna(corr):
        return None
    return round(float(corr), 4)



def _spearman_corr(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or len(ys) < 3 or len(xs) != len(ys):
        return None
    sx = pd.Series(xs, dtype=float)
    sy = pd.Series(ys, dtype=float)
    # Avoid optional SciPy dependency on platforms where pandas may delegate
    # Spearman calculation to SciPy. Rank both series explicitly, then compute
    # ordinary Pearson correlation on the ranks.
    sx_rank = sx.rank(method="average")
    sy_rank = sy.rank(method="average")
    corr = sx_rank.corr(sy_rank)
    if corr is None or pd.isna(corr):
        return None
    return round(float(corr), 4)



def _sort_score_band_key(label: str) -> Tuple[int, str]:
    try:
        lo = int(str(label).split("-")[0].replace("%", ""))
    except Exception:
        lo = 999
    return lo, str(label)



def _score_band(score: float) -> str:
    bands = [0, 40, 50, 60, 70, 80, 90, 101]
    labels = ["0-39", "40-49", "50-59", "60-69", "70-79", "80-89", "90-100"]
    for idx in range(len(labels)):
        if bands[idx] <= score < bands[idx + 1]:
            return labels[idx]
    return labels[-1]



def _reliability_bin(prob: float) -> str:
    pct = int(round(prob * 100))
    lo = (pct // 10) * 10
    hi = min(lo + 9, 100)
    return f"{lo}-{hi}%"



def _top_vs_rest_rows(observations: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if observations.empty:
        return rows
    quantile_sets = {
        "top_decile": observations[observations["score_percentile"] >= 90],
        "top_quintile": observations[observations["score_percentile"] >= 80],
        "middle_40_60": observations[(observations["score_percentile"] >= 40) & (observations["score_percentile"] < 60)],
        "bottom_decile": observations[observations["score_percentile"] < 10],
        "all": observations,
    }
    base = quantile_sets["all"]
    base_hit = float(base["target_hit"].mean()) if len(base) else 0.0
    for label, df in quantile_sets.items():
        if df.empty:
            continue
        hit = float(df["target_hit"].mean())
        rows.append({
            "bucket": label,
            "observations": int(len(df)),
            "hit_rate": round(hit, 4),
            "avg_end_return_pct": _safe_round(df["end_return_pct"].mean()),
            "avg_max_return_pct": _safe_round(df["max_return_pct"].mean()),
            "avg_min_return_pct": _safe_round(df["min_return_pct"].mean()),
            "lift_vs_all": round(hit - base_hit, 4),
        })
    return rows



def _quantile_lift_rows(observations: pd.DataFrame, bins: int = 10) -> List[Dict[str, Any]]:
    if observations.empty:
        return []
    df = observations.copy()
    ranked = df["score"].rank(method="first")
    df["score_decile"] = pd.qcut(ranked, q=min(bins, len(df)), labels=False, duplicates="drop")
    base_hit = float(df["target_hit"].mean()) if len(df) else 0.0
    rows: List[Dict[str, Any]] = []
    if df["score_decile"].isna().all():
        return rows
    max_decile = int(df["score_decile"].max())
    for decile, group in df.groupby("score_decile"):
        bucket_num = int(decile) + 1
        label = f"D{bucket_num:02d}"
        rows.append({
            "quantile": label,
            "quantile_index": bucket_num,
            "quantile_from_low": bucket_num,
            "quantile_from_high": max_decile - int(decile) + 1,
            "observations": int(len(group)),
            "avg_score": _safe_round(group["score"].mean(), 2),
            "hit_rate": round(float(group["target_hit"].mean()), 4),
            "lift_vs_all": round(float(group["target_hit"].mean()) - base_hit, 4),
            "avg_end_return_pct": _safe_round(group["end_return_pct"].mean()),
            "avg_max_return_pct": _safe_round(group["max_return_pct"].mean()),
            "avg_min_return_pct": _safe_round(group["min_return_pct"].mean()),
        })
    return sorted(rows, key=lambda row: row["quantile_index"])



def _monotonicity_diagnostics(score_band_metrics: List[Dict[str, Any]], tolerance: float) -> Dict[str, Any]:
    ordered = sorted(score_band_metrics, key=lambda row: _sort_score_band_key(row.get("score_band", "")))
    violations: List[Dict[str, Any]] = []
    previous = None
    for row in ordered:
        if previous is not None:
            prev_hit = float(previous.get("hit_rate", 0.0))
            curr_hit = float(row.get("hit_rate", 0.0))
            if curr_hit + tolerance < prev_hit:
                violations.append({
                    "from_band": previous.get("score_band"),
                    "to_band": row.get("score_band"),
                    "from_hit_rate": round(prev_hit, 4),
                    "to_hit_rate": round(curr_hit, 4),
                    "drop": round(curr_hit - prev_hit, 4),
                })
        previous = row
    return {
        "score_band_sequence": [row.get("score_band") for row in ordered],
        "score_band_hit_rates": [{"score_band": row.get("score_band"), "hit_rate": row.get("hit_rate"), "observations": row.get("observations")} for row in ordered],
        "tolerance": tolerance,
        "violation_count": len(violations),
        "violations": violations,
    }



def _benchmark_regime_metrics(spy_hist: pd.DataFrame) -> Dict[str, Any]:
    if spy_hist is None or spy_hist.empty or "Close" not in spy_hist.columns:
        return {
            "regime_label": "unknown",
            "benchmark_close": None,
            "benchmark_one_month_return": None,
            "benchmark_three_month_return": None,
            "benchmark_rsi14": None,
            "benchmark_above_50d": None,
            "benchmark_above_200d": None,
            "benchmark_volatility20": None,
        }
    close = spy_hist["Close"].dropna()
    if close.empty:
        return {
            "regime_label": "unknown",
            "benchmark_close": None,
            "benchmark_one_month_return": None,
            "benchmark_three_month_return": None,
            "benchmark_rsi14": None,
            "benchmark_above_50d": None,
            "benchmark_above_200d": None,
            "benchmark_volatility20": None,
        }
    latest = float(close.iloc[-1])
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi14 = 100 - (100 / (1 + rs))
    daily_ret = close.pct_change()
    vol20 = float(daily_ret.rolling(20).std().iloc[-1] * (252 ** 0.5)) if len(close) >= 21 else None
    one_month = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) > 21 else None
    three_month = float(close.iloc[-1] / close.iloc[-63] - 1.0) if len(close) > 63 else None
    above_50 = bool(pd.notna(sma50.iloc[-1]) and latest > float(sma50.iloc[-1])) if len(close) >= 50 else None
    above_200 = bool(pd.notna(sma200.iloc[-1]) and latest > float(sma200.iloc[-1])) if len(close) >= 200 else None
    rsi_val = float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else None

    regime = "neutral"
    if above_50 and above_200 and (one_month is not None and one_month > 0) and (rsi_val is None or rsi_val >= 50):
        regime = "risk_on"
    elif (above_50 is False and above_200 is False) or (one_month is not None and one_month < -0.04) or (rsi_val is not None and rsi_val < 45):
        regime = "risk_off"

    return {
        "regime_label": regime,
        "benchmark_close": _safe_round(latest, 4),
        "benchmark_one_month_return": _safe_round(one_month),
        "benchmark_three_month_return": _safe_round(three_month),
        "benchmark_rsi14": _safe_round(rsi_val, 2),
        "benchmark_above_50d": above_50,
        "benchmark_above_200d": above_200,
        "benchmark_volatility20": _safe_round(vol20),
    }



def _context_adjustment(metrics: Dict[str, Any], regime: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
    regime_label = regime.get("regime_label") or "neutral"
    adjustment = 0.0
    reasons: List[str] = []
    risks: List[str] = []

    rs_spy = metrics.get("relative_strength_vs_spy")
    rs_sector = metrics.get("relative_strength_vs_sector")
    one_month = metrics.get("one_month_return")
    three_month = metrics.get("three_month_return")
    rsi = metrics.get("rsi14")
    volume_ratio = metrics.get("volume_ratio")
    latest = metrics.get("latest_close")
    sma50 = metrics.get("sma50")
    sma200 = metrics.get("sma200")
    breakout = bool(metrics.get("breakout"))
    dist_to_high = metrics.get("dist_to_52w_high")

    if regime_label == "risk_on":
        if rs_spy is not None and rs_spy > 0.03:
            adjustment += 2.5
            reasons.append("Risk-on regime with stock outperforming SPY")
        if rs_sector is not None and rs_sector > 0.02:
            adjustment += 1.5
            reasons.append("Risk-on regime with sector-relative strength")
        if breakout and (volume_ratio or 0) >= 1.1:
            adjustment += 1.5
            reasons.append("Breakout confirmation in supportive tape")
        if latest and sma50 and sma200 and latest > sma50 > sma200:
            adjustment += 1.0
            reasons.append("Trend stack aligned with supportive benchmark")
    elif regime_label == "risk_off":
        if rs_spy is not None and rs_spy < 0:
            adjustment -= 2.5
            risks.append("Risk-off regime and stock lagging SPY")
        if latest and sma50 and latest < sma50:
            adjustment -= 1.5
            risks.append("Risk-off regime with price below 50-day average")
        if one_month is not None and one_month < 0:
            adjustment -= 1.0
            risks.append("Risk-off regime with negative 1-month momentum")
        if rsi is not None and rsi > 74:
            adjustment -= 1.0
            risks.append("Risk-off regime with stretched RSI")
    else:
        if rs_spy is not None and rs_spy > 0.04 and three_month is not None and three_month > 0.08:
            adjustment += 1.5
            reasons.append("Neutral tape but standout relative strength")
        if rs_sector is not None and rs_sector < 0:
            adjustment -= 1.0
            risks.append("Neutral tape but lagging sector ETF")

    if dist_to_high is not None and dist_to_high > -0.005 and rsi is not None and rsi > 74:
        adjustment -= 1.5
        risks.append("Very extended versus 52-week highs")
    if volume_ratio is not None and volume_ratio < 0.75 and breakout:
        adjustment -= 1.0
        risks.append("Breakout lacks convincing volume confirmation")

    return round(adjustment, 2), reasons[:3], risks[:3]


def _trailing_return(series: pd.Series, periods: int) -> Optional[float]:
    if series is None or len(series) <= periods:
        return None
    try:
        start = float(series.iloc[-(periods + 1)])
        end = float(series.iloc[-1])
        if start == 0:
            return None
        return float(end / start - 1.0)
    except Exception:
        return None


def _relative_trailing_return(asset_close: pd.Series, benchmark_close: Optional[pd.Series], periods: int) -> Optional[float]:
    if benchmark_close is None or benchmark_close.empty:
        return None
    asset_ret = _trailing_return(asset_close, periods)
    bench_ret = _trailing_return(benchmark_close, periods)
    if asset_ret is None or bench_ret is None:
        return None
    return float(asset_ret - bench_ret)


def _rolling_slope_pct(series: pd.Series, periods: int) -> Optional[float]:
    if series is None or len(series.dropna()) <= periods:
        return None
    tail = series.dropna()
    try:
        start = float(tail.iloc[-(periods + 1)])
        end = float(tail.iloc[-1])
        if start == 0:
            return None
        return float(end / start - 1.0)
    except Exception:
        return None


def _trend_efficiency(close: pd.Series, periods: int = 20) -> Optional[float]:
    if close is None or len(close) <= periods:
        return None
    window = close.tail(periods + 1)
    if len(window) <= periods:
        return None
    path = window.diff().abs().sum()
    if path is None or pd.isna(path) or float(path) == 0.0:
        return None
    net = abs(float(window.iloc[-1]) - float(window.iloc[0]))
    return float(net / float(path))


def _surface_feature_metrics(hist: pd.DataFrame, spy_hist: pd.DataFrame, sector_hist: Optional[pd.DataFrame]) -> Dict[str, Any]:
    close = hist["Close"].dropna()
    low = hist["Low"].dropna() if "Low" in hist.columns else close
    returns = close.pct_change().dropna()
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    latest = float(close.iloc[-1]) if not close.empty else None

    spy_close = spy_hist["Close"].dropna() if spy_hist is not None and not spy_hist.empty and "Close" in spy_hist.columns else None
    sector_close = sector_hist["Close"].dropna() if sector_hist is not None and not sector_hist.empty and "Close" in sector_hist.columns else None

    vol20 = float(returns.tail(20).std()) if len(returns) >= 20 else None
    vol60 = float(returns.tail(60).std()) if len(returns) >= 60 else None
    compression_ratio = None
    if vol20 is not None and vol60 not in (None, 0):
        compression_ratio = float(vol20 / vol60)

    recent_close = close.tail(6)
    recent_sma20 = sma20.tail(6)
    reclaimed_20d = False
    if latest is not None and len(recent_close) >= 2 and len(recent_sma20.dropna()) >= 2:
        prior_below = bool((recent_close.iloc[:-1] < recent_sma20.iloc[:-1]).fillna(False).any())
        reclaimed_20d = bool(latest > float(recent_sma20.iloc[-1]) and prior_below)

    return {
        "return_5d": _trailing_return(close, 5),
        "return_10d": _trailing_return(close, 10),
        "return_20d": _trailing_return(close, 20),
        "return_60d": _trailing_return(close, 60),
        "drawdown_20d_high": float(latest / float(close.tail(20).max()) - 1.0) if latest is not None and len(close) >= 20 else None,
        "drawdown_63d_high": float(latest / float(close.tail(63).max()) - 1.0) if latest is not None and len(close) >= 63 else None,
        "rebound_from_10d_low": float(latest / float(low.tail(10).min()) - 1.0) if latest is not None and len(low) >= 10 else None,
        "trend_efficiency_20": _trend_efficiency(close, 20),
        "sma20_slope_5d": _rolling_slope_pct(sma20, 5),
        "sma50_slope_10d": _rolling_slope_pct(sma50, 10),
        "compression_ratio_20_60": compression_ratio,
        "positive_days_10": float((returns.tail(10) > 0).mean()) if len(returns) >= 10 else None,
        "rs_spy_5d": _relative_trailing_return(close, spy_close, 5),
        "rs_spy_20d": _relative_trailing_return(close, spy_close, 20),
        "rs_sector_20d": _relative_trailing_return(close, sector_close, 20),
        "rs_acceleration": (lambda rs5, rs20: (float(rs5 - rs20) if rs5 is not None and rs20 is not None else None))(_relative_trailing_return(close, spy_close, 5), _relative_trailing_return(close, spy_close, 20)),
        "reclaimed_20d": reclaimed_20d,
        "distance_vs_sma20": float(latest / float(sma20.iloc[-1]) - 1.0) if latest is not None and pd.notna(sma20.iloc[-1]) else None,
        "distance_vs_sma50": float(latest / float(sma50.iloc[-1]) - 1.0) if latest is not None and pd.notna(sma50.iloc[-1]) else None,
        "distance_vs_sma200": float(latest / float(sma200.iloc[-1]) - 1.0) if latest is not None and pd.notna(sma200.iloc[-1]) else None,
    }





def _score_continuation_surface(metrics: Dict[str, Any], surface: Dict[str, Any], regime: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
    score = 35.0
    reasons: List[str] = []
    risks: List[str] = []

    latest = metrics.get("latest_close")
    sma50 = metrics.get("sma50")
    sma200 = metrics.get("sma200")
    one_month = metrics.get("one_month_return")
    three_month = metrics.get("three_month_return")
    rsi = metrics.get("rsi14")
    volume_ratio = metrics.get("volume_ratio")
    rs_spy = metrics.get("relative_strength_vs_spy")
    rs_sector = metrics.get("relative_strength_vs_sector")

    slope20 = surface.get("sma20_slope_5d")
    slope50 = surface.get("sma50_slope_10d")
    eff20 = surface.get("trend_efficiency_20")
    compress = surface.get("compression_ratio_20_60")
    dd20 = surface.get("drawdown_20d_high")
    ret5 = surface.get("return_5d")
    pos10 = surface.get("positive_days_10")

    if latest and sma50 and sma200 and latest > sma50 > sma200:
        score += 12
        reasons.append("Trend stack aligned above 50d and 200d")
    elif latest and sma50 and latest > sma50:
        score += 6
        reasons.append("Price holding above 50d average")
    else:
        score -= 6
        risks.append("Continuation setup lacks moving-average support")

    if slope20 is not None:
        if slope20 > 0.015:
            score += 8
            reasons.append("20d trend slope rising decisively")
        elif slope20 > 0.0:
            score += 4
        else:
            score -= 4
            risks.append("20d trend slope not rising")
    if slope50 is not None:
        if slope50 > 0.02:
            score += 8
            reasons.append("50d trend slope supportive")
        elif slope50 < 0:
            score -= 5
            risks.append("50d trend slope negative")

    if eff20 is not None:
        if eff20 >= 0.55:
            score += 8
            reasons.append("Trend efficiency looks clean")
        elif eff20 >= 0.4:
            score += 4
        elif eff20 < 0.22:
            score -= 6
            risks.append("Recent path looks choppy")

    if compress is not None:
        if 0.55 <= compress <= 0.95:
            score += 6
            reasons.append("Recent volatility compression constructive")
        elif compress > 1.25:
            score -= 4
            risks.append("Recent volatility expansion reduces continuation quality")

    if one_month is not None:
        if one_month > 0.03:
            score += 4
        elif one_month < -0.02:
            score -= 5
            risks.append("1m momentum still negative for continuation")
    if three_month is not None:
        if three_month > 0.08:
            score += 6
            reasons.append("3m momentum remains supportive")
        elif three_month < -0.05:
            score -= 5
            risks.append("3m momentum too weak for continuation")

    if rs_spy is not None:
        if rs_spy > 0.02:
            score += 5
            reasons.append("Still outperforming SPY")
        elif rs_spy < -0.02:
            score -= 5
            risks.append("Lagging SPY weakens continuation")
    if rs_sector is not None:
        if rs_sector > 0.015:
            score += 3
        elif rs_sector < 0:
            score -= 3

    if dd20 is not None:
        if -0.10 <= dd20 <= -0.02:
            score += 5
            reasons.append("Pullback depth still constructive")
        elif dd20 > -0.01 and rsi is not None and rsi > 72:
            score -= 4
            risks.append("Continuation setup looks extended")

    if ret5 is not None:
        if 0 < ret5 < 0.08:
            score += 3
        elif ret5 > 0.12 and rsi is not None and rsi > 72:
            score -= 4
            risks.append("Very strong 5d move may be overcrowded")
        elif ret5 < -0.04:
            score -= 4
            risks.append("Recent tape weak for continuation")

    if volume_ratio is not None and volume_ratio > 1.05:
        score += 3
    if pos10 is not None and pos10 >= 0.6:
        score += 2

    regime_label = regime.get("regime_label") or "neutral"
    if regime_label == "risk_on" and latest and sma50 and sma200 and latest > sma50 > sma200:
        score += 4
    if regime_label == "risk_off" and (rs_spy is None or rs_spy < 0.03):
        score -= 6
        risks.append("Risk-off tape demands stronger relative strength")

    return round(max(0.0, min(100.0, score)), 2), reasons[:4], risks[:4]


def _score_rebound_surface(metrics: Dict[str, Any], surface: Dict[str, Any], regime: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
    score = 20.0
    reasons: List[str] = []
    risks: List[str] = []

    latest = metrics.get("latest_close")
    sma200 = metrics.get("sma200")
    rsi = metrics.get("rsi14")
    ret5 = surface.get("return_5d")
    ret20 = surface.get("return_20d")
    dd63 = surface.get("drawdown_63d_high")
    rebound10 = surface.get("rebound_from_10d_low")
    reclaim20 = bool(surface.get("reclaimed_20d"))
    rs_delta = surface.get("rs_acceleration")
    compress = surface.get("compression_ratio_20_60")
    rs5 = surface.get("rs_spy_5d")

    if dd63 is not None:
        if -0.18 <= dd63 <= -0.05:
            score += 12
            reasons.append("Pullback depth is reset but not broken")
        elif dd63 < -0.25:
            score -= 8
            risks.append("Pullback may be too deep for a clean rebound")

    if ret20 is not None and ret5 is not None:
        if ret20 < -0.03 and ret5 > 0.02:
            score += 12
            reasons.append("Short-term reversal forming after pullback")
        elif ret20 < -0.08 and ret5 <= 0:
            score -= 6
            risks.append("No rebound confirmation yet")

    if reclaim20:
        score += 10
        reasons.append("Price reclaimed 20d average")
    if rs_delta is not None:
        if rs_delta > 0.02:
            score += 8
            reasons.append("Relative strength is improving")
        elif rs_delta < -0.02:
            score -= 5
            risks.append("Relative strength still deteriorating")

    if rebound10 is not None:
        if 0.03 <= rebound10 <= 0.1:
            score += 6
            reasons.append("Bounce off recent lows looks usable")
        elif rebound10 > 0.14:
            score += 2
        elif rebound10 < 0.01:
            score -= 4
            risks.append("Bounce off recent lows still weak")

    if rsi is not None:
        if 40 <= rsi <= 58:
            score += 8
            reasons.append("RSI reset supports rebound potential")
        elif 35 <= rsi < 40:
            score += 4
        elif rsi < 30:
            score -= 6
            risks.append("RSI still too damaged")
        elif rsi > 68:
            score -= 3
            risks.append("Rebound may already be well progressed")

    if latest and sma200:
        if latest > sma200 or (dd63 is not None and dd63 > -0.15):
            score += 4
        else:
            score -= 6
            risks.append("Below 200d with deep drawdown")

    regime_label = regime.get("regime_label") or "neutral"
    if regime_label == "risk_off" and rs5 is not None and rs5 > 0:
        score += 6
        reasons.append("Risk-off tape rewards defensive relative rebound")
    if regime_label == "risk_on" and ret20 is not None and ret20 < -0.12:
        score -= 4
        risks.append("Risk-on tape favors stronger trends than this rebound")

    if compress is not None and compress > 1.35:
        score -= 4
        risks.append("Rebound is arriving with unstable volatility")

    return round(max(0.0, min(100.0, score)), 2), reasons[:4], risks[:4]


def _blend_surface_score_v22(raw_score: float, continuation_score: float, rebound_score: float, context_adjustment: float, regime: Dict[str, Any]) -> Tuple[float, float, str]:
    regime_label = regime.get("regime_label") or "neutral"
    if continuation_score >= rebound_score + 8:
        label = "continuation"
        score = (0.60 * raw_score) + (0.30 * continuation_score) + (0.10 * rebound_score)
    elif rebound_score >= continuation_score + 8:
        label = "rebound"
        score = (0.35 * raw_score) + (0.55 * rebound_score) + (0.10 * continuation_score)
    else:
        label = "blended"
        score = (0.45 * raw_score) + (0.35 * continuation_score) + (0.20 * rebound_score)

    if regime_label == "risk_on" and label == "continuation":
        score += 2.0
    elif regime_label == "risk_off" and label == "rebound":
        score += 2.5
    elif regime_label == "risk_off" and label == "continuation":
        score -= 2.5

    surface_score = continuation_score if label == "continuation" else rebound_score if label == "rebound" else round((continuation_score + rebound_score) / 2.0, 2)
    score += context_adjustment
    score = max(0.0, min(100.0, score))
    return round(score, 2), round(surface_score, 2), label


def _build_surface_feature_report(observations: pd.DataFrame) -> Dict[str, Any]:
    if observations.empty:
        return {
            "status": "empty",
            "surface_label_distribution": {},
            "surface_label_metrics": [],
            "surface_by_regime": [],
            "score_component_summary": {},
        }

    label_dist = observations.get("surface_label")
    distribution = {}
    if label_dist is not None:
        counts = label_dist.fillna("unknown").value_counts().to_dict()
        distribution = {str(k): int(v) for k, v in counts.items()}

    label_metrics = []
    if "surface_label" in observations.columns:
        for label, group in observations.groupby("surface_label"):
            label_metrics.append({
                "surface_label": label,
                "observations": int(len(group)),
                "hit_rate": _safe_round(group["target_hit"].mean()),
                "avg_score": _safe_round(group["score"].mean(), 2),
                "avg_raw_score": _safe_round(group["raw_score"].mean(), 2) if "raw_score" in group.columns else None,
                "avg_surface_score": _safe_round(group["surface_score"].mean(), 2) if "surface_score" in group.columns else None,
                "avg_continuation_score": _safe_round(group["continuation_score"].mean(), 2) if "continuation_score" in group.columns else None,
                "avg_rebound_score": _safe_round(group["rebound_score"].mean(), 2) if "rebound_score" in group.columns else None,
                "avg_end_return_pct": _safe_round(group["end_return_pct"].mean()),
            })
    label_metrics = sorted(label_metrics, key=lambda row: row["observations"], reverse=True)

    surface_by_regime = []
    if "surface_label" in observations.columns and "market_regime" in observations.columns:
        for (regime_label, surface_label), group in observations.groupby(["market_regime", "surface_label"]):
            if len(group) < 50:
                continue
            surface_by_regime.append({
                "market_regime": regime_label,
                "surface_label": surface_label,
                "observations": int(len(group)),
                "hit_rate": _safe_round(group["target_hit"].mean()),
                "avg_score": _safe_round(group["score"].mean(), 2),
                "avg_end_return_pct": _safe_round(group["end_return_pct"].mean()),
            })
    surface_by_regime = sorted(surface_by_regime, key=lambda row: (row["market_regime"], row["surface_label"]))

    component_summary = {
        "avg_raw_score": _safe_round(observations["raw_score"].mean(), 2) if "raw_score" in observations.columns else None,
        "avg_context_adjustment": _safe_round(observations["context_adjustment"].mean(), 2) if "context_adjustment" in observations.columns else None,
        "avg_surface_score": _safe_round(observations["surface_score"].mean(), 2) if "surface_score" in observations.columns else None,
        "avg_continuation_score": _safe_round(observations["continuation_score"].mean(), 2) if "continuation_score" in observations.columns else None,
        "avg_rebound_score": _safe_round(observations["rebound_score"].mean(), 2) if "rebound_score" in observations.columns else None,
    }

    return {
        "status": "ok",
        "surface_label_distribution": distribution,
        "surface_label_metrics": label_metrics,
        "surface_by_regime": surface_by_regime,
        "score_component_summary": component_summary,
    }

def _regime_slice_rows(observations: pd.DataFrame, min_obs: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if observations.empty or "market_regime" not in observations.columns:
        return rows
    for regime_label, group in observations.groupby("market_regime"):
        if len(group) < min_obs:
            continue
        hit = float(group["target_hit"].mean())
        top = group[group["score_percentile"] >= 90]
        top_hit = float(top["target_hit"].mean()) if len(top) else None
        rows.append({
            "market_regime": regime_label,
            "observations": int(len(group)),
            "hit_rate": round(hit, 4),
            "top_decile_hit_rate": _safe_round(top_hit),
            "top_decile_lift": _safe_round((top_hit - hit) if top_hit is not None else None),
            "avg_score": _safe_round(group["score"].mean(), 2),
            "avg_end_return_pct": _safe_round(group["end_return_pct"].mean()),
        })
    return sorted(rows, key=lambda row: row["market_regime"])



def _evaluate_forward_outcome(frame: pd.DataFrame, snapshot_date: pd.Timestamp, entry_price: float, target_up_pct: float, stop_down_pct: float, horizon_days: int) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {
            "status": "insufficient_data",
            "hit_up_first": 0,
            "hit_down_first": 0,
            "max_return_pct": None,
            "min_return_pct": None,
            "end_return_pct": None,
            "days_elapsed": 0,
            "outcome_note": "No price history",
        }
    future = frame[frame.index > snapshot_date].head(horizon_days)
    if future.empty:
        return {
            "status": "insufficient_data",
            "hit_up_first": 0,
            "hit_down_first": 0,
            "max_return_pct": None,
            "min_return_pct": None,
            "end_return_pct": None,
            "days_elapsed": 0,
            "outcome_note": "No forward rows",
        }
    max_return_pct = float((future["High"].max() / entry_price) - 1.0)
    min_return_pct = float((future["Low"].min() / entry_price) - 1.0)
    end_return_pct = float((future["Close"].iloc[-1] / entry_price) - 1.0)
    hit_up_idx = None
    hit_down_idx = None
    for idx, candle in future.iterrows():
        if hit_up_idx is None and float(candle["High"]) >= entry_price * (1 + target_up_pct):
            hit_up_idx = idx
        if hit_down_idx is None and float(candle["Low"]) <= entry_price * (1 - stop_down_pct):
            hit_down_idx = idx
        if hit_up_idx is not None and hit_down_idx is not None:
            break
    status = "expired"
    note = "Reached horizon without touching target or stop first"
    hit_up_first = 0
    hit_down_first = 0
    if hit_up_idx is not None and (hit_down_idx is None or hit_up_idx <= hit_down_idx):
        status = "target_hit"
        note = "Touched +5% before -3%"
        hit_up_first = 1
    elif hit_down_idx is not None and (hit_up_idx is None or hit_down_idx < hit_up_idx):
        status = "stop_hit"
        note = "Touched -3% before +5%"
        hit_down_first = 1
    return {
        "status": status,
        "hit_up_first": hit_up_first,
        "hit_down_first": hit_down_first,
        "max_return_pct": round(max_return_pct, 4),
        "min_return_pct": round(min_return_pct, 4),
        "end_return_pct": round(end_return_pct, 4),
        "days_elapsed": min(len(future), horizon_days),
        "outcome_note": note,
    }




ELITE_POLICY_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "policy_id": "top_3_per_snapshot",
        "label": "Top 3 per snapshot",
        "description": "Only the three highest-scoring replay candidates each snapshot.",
        "top_n": 3,
    },
    {
        "policy_id": "top_5_per_snapshot",
        "label": "Top 5 per snapshot",
        "description": "Only the five highest-scoring replay candidates each snapshot.",
        "top_n": 5,
    },
    {
        "policy_id": "top_10_per_snapshot",
        "label": "Top 10 per snapshot",
        "description": "Only the ten highest-scoring replay candidates each snapshot.",
        "top_n": 10,
    },
    {
        "policy_id": "score_ge_85",
        "label": "Score >= 85",
        "description": "Any replay candidate with score at or above 85.",
        "min_score": 85,
    },
    {
        "policy_id": "score_ge_90",
        "label": "Score >= 90",
        "description": "Any replay candidate with score at or above 90.",
        "min_score": 90,
    },
    {
        "policy_id": "score_ge_80_risk_on",
        "label": "Score >= 80 in risk-on regime",
        "description": "High score candidate when benchmark context is risk-on.",
        "min_score": 80,
        "market_regime": "risk_on",
    },
    {
        "policy_id": "score_ge_80_risk_off",
        "label": "Score >= 80 in risk-off regime",
        "description": "High score candidate when benchmark context is risk-off.",
        "min_score": 80,
        "market_regime": "risk_off",
    },
    {
        "policy_id": "score_ge_80_continuation",
        "label": "Score >= 80 + continuation surface",
        "description": "High score candidate classified as a continuation setup.",
        "min_score": 80,
        "surface_label": "continuation",
    },
    {
        "policy_id": "score_ge_85_continuation",
        "label": "Score >= 85 + continuation surface",
        "description": "Elite score candidate classified as a continuation setup.",
        "min_score": 85,
        "surface_label": "continuation",
    },
    {
        "policy_id": "score_ge_90_continuation",
        "label": "Score >= 90 + continuation surface",
        "description": "Very elite score candidate classified as a continuation setup.",
        "min_score": 90,
        "surface_label": "continuation",
    },
    {
        "policy_id": "score_ge_80_rebound",
        "label": "Score >= 80 + rebound surface",
        "description": "High score candidate classified as a rebound setup.",
        "min_score": 80,
        "surface_label": "rebound",
    },
    {
        "policy_id": "score_ge_85_rebound",
        "label": "Score >= 85 + rebound surface",
        "description": "Elite score candidate classified as a rebound setup.",
        "min_score": 85,
        "surface_label": "rebound",
    },
    {
        "policy_id": "score_ge_80_risk_on_continuation",
        "label": "Score >= 80 + risk-on continuation",
        "description": "High score continuation candidate in a supportive benchmark regime.",
        "min_score": 80,
        "market_regime": "risk_on",
        "surface_label": "continuation",
    },
    {
        "policy_id": "score_ge_80_risk_off_rebound",
        "label": "Score >= 80 + risk-off rebound",
        "description": "High score rebound candidate in a risk-off benchmark regime.",
        "min_score": 80,
        "market_regime": "risk_off",
        "surface_label": "rebound",
    },
]


def _policy_mask(observations: pd.DataFrame, policy: Dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=observations.index)
    if "top_n" in policy:
        mask &= observations.get("score_rank", pd.Series(index=observations.index, dtype=float)) <= int(policy["top_n"])
    if "min_score" in policy:
        mask &= observations.get("score", pd.Series(index=observations.index, dtype=float)) >= float(policy["min_score"])
    if policy.get("market_regime"):
        mask &= observations.get("market_regime", pd.Series(index=observations.index, dtype=str)).astype(str) == str(policy["market_regime"])
    if policy.get("surface_label"):
        mask &= observations.get("surface_label", pd.Series(index=observations.index, dtype=str)).astype(str) == str(policy["surface_label"])
    return mask.fillna(False)


def _policy_pass_status(row: Dict[str, Any], settings) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    if row["observations"] < settings.replay_policy_min_observations:
        reasons.append(f"Only {row['observations']} observations; minimum is {settings.replay_policy_min_observations}.")
    if row["snapshot_count"] < settings.replay_policy_min_snapshots:
        reasons.append(f"Only {row['snapshot_count']} snapshots represented; minimum is {settings.replay_policy_min_snapshots}.")
    if row["lift_vs_all"] is None or row["lift_vs_all"] < settings.replay_policy_min_lift_vs_all:
        reasons.append(f"Lift vs all {row['lift_vs_all']} is below {settings.replay_policy_min_lift_vs_all}.")
    if row["avg_end_return_pct"] is None or row["avg_end_return_pct"] < settings.replay_policy_min_avg_end_return_pct:
        reasons.append(f"Average end return {row['avg_end_return_pct']} is below {settings.replay_policy_min_avg_end_return_pct}.")
    if row["stop_rate"] is not None and row["stop_rate"] > settings.replay_policy_max_stop_rate:
        reasons.append(f"Stop rate {row['stop_rate']} is above {settings.replay_policy_max_stop_rate}.")
    if reasons:
        if row["observations"] >= settings.replay_policy_min_observations and row["snapshot_count"] >= settings.replay_policy_min_snapshots and (row["lift_vs_all"] or 0) > 0 and (row["avg_end_return_pct"] or 0) > 0:
            return "watchlist", reasons
        return "fail", reasons
    return "pass", ["Policy passes the V2.3 elite-policy gates. This is a historical policy gate, not a calibrated probability."]


def _build_elite_policy_outputs(observations: pd.DataFrame, settings) -> Dict[str, Any]:
    if observations.empty:
        empty_report = {
            "status": "failed",
            "reason": "No observations available for elite-policy validation.",
            "policy_gates": {},
            "recommended_policy_count": 0,
            "recommended_policies": [],
            "best_policy": None,
            "note": "Policy validation is not probability calibration.",
        }
        return {"leaderboard": [], "report": empty_report}

    df = observations.copy()
    snapshot_count = int(df["snapshot_date"].nunique()) if "snapshot_date" in df.columns else 0
    all_hit = float(df["target_hit"].mean()) if len(df) else 0.0
    all_end = float(df["end_return_pct"].mean()) if "end_return_pct" in df.columns and len(df) else 0.0
    shortlist_df = df[df.get("shortlist_flag", 0) == 1]
    shortlist_hit = float(shortlist_df["target_hit"].mean()) if len(shortlist_df) else all_hit

    leaderboard: List[Dict[str, Any]] = []
    for definition in ELITE_POLICY_DEFINITIONS:
        selected = df[_policy_mask(df, definition)].copy()
        if selected.empty:
            row = {
                "policy_id": definition["policy_id"],
                "label": definition["label"],
                "description": definition["description"],
                "observations": 0,
                "snapshot_count": 0,
                "snapshot_coverage_pct": 0.0,
                "avg_candidates_per_snapshot": 0.0,
                "hit_rate": None,
                "lift_vs_all": None,
                "lift_vs_shortlist": None,
                "avg_end_return_pct": None,
                "avg_max_return_pct": None,
                "avg_min_return_pct": None,
                "stop_rate": None,
                "expired_rate": None,
                "median_score": None,
                "min_score": definition.get("min_score"),
                "max_score": None,
                "dominant_regime": definition.get("market_regime") or "mixed",
                "dominant_surface": definition.get("surface_label") or "mixed",
                "policy_score": -999.0,
                "validation_status": "fail",
                "validation_reason": "No observations matched this policy.",
            }
            leaderboard.append(row)
            continue

        obs = int(len(selected))
        selected_snapshots = int(selected["snapshot_date"].nunique()) if "snapshot_date" in selected.columns else 0
        hit_rate = float(selected["target_hit"].mean())
        stop_rate = float(selected["stop_hit"].mean()) if "stop_hit" in selected.columns else None
        expired_rate = float(selected["expired_flag"].mean()) if "expired_flag" in selected.columns else None
        lift_vs_all = hit_rate - all_hit
        lift_vs_shortlist = hit_rate - shortlist_hit
        regime_mode = selected["market_regime"].mode().iloc[0] if "market_regime" in selected.columns and not selected["market_regime"].mode().empty else None
        surface_mode = selected["surface_label"].mode().iloc[0] if "surface_label" in selected.columns and not selected["surface_label"].mode().empty else None
        avg_end = float(selected["end_return_pct"].mean()) if "end_return_pct" in selected.columns else None
        avg_max = float(selected["max_return_pct"].mean()) if "max_return_pct" in selected.columns else None
        avg_min = float(selected["min_return_pct"].mean()) if "min_return_pct" in selected.columns else None
        # A compact ranking score for operator ordering. It favors lift, returns, and enough sample size, while penalizing high stop rates.
        sample_factor = min(1.0, obs / max(float(settings.replay_policy_min_observations), 1.0))
        policy_score = (lift_vs_all * 100.0) + ((avg_end or 0.0) * 50.0) + (hit_rate * 10.0) + (sample_factor * 2.0) - ((stop_rate or 0.0) * 3.0)
        row = {
            "policy_id": definition["policy_id"],
            "label": definition["label"],
            "description": definition["description"],
            "observations": obs,
            "snapshot_count": selected_snapshots,
            "snapshot_coverage_pct": _safe_round((selected_snapshots / snapshot_count) if snapshot_count else 0.0),
            "avg_candidates_per_snapshot": _safe_round(obs / selected_snapshots, 2) if selected_snapshots else 0.0,
            "hit_rate": _safe_round(hit_rate),
            "lift_vs_all": _safe_round(lift_vs_all),
            "lift_vs_shortlist": _safe_round(lift_vs_shortlist),
            "avg_end_return_pct": _safe_round(avg_end),
            "avg_max_return_pct": _safe_round(avg_max),
            "avg_min_return_pct": _safe_round(avg_min),
            "stop_rate": _safe_round(stop_rate),
            "expired_rate": _safe_round(expired_rate),
            "median_score": _safe_round(selected["score"].median(), 2),
            "min_score": _safe_round(selected["score"].min(), 2),
            "max_score": _safe_round(selected["score"].max(), 2),
            "dominant_regime": regime_mode or definition.get("market_regime") or "mixed",
            "dominant_surface": surface_mode or definition.get("surface_label") or "mixed",
            "policy_score": _safe_round(policy_score, 3),
        }
        status, reasons = _policy_pass_status(row, settings)
        row["validation_status"] = status
        row["validation_reason"] = " | ".join(reasons[:3])
        leaderboard.append(row)

    status_order = {"pass": 3, "watchlist": 2, "fail": 1}
    leaderboard = sorted(leaderboard, key=lambda r: (status_order.get(str(r.get("validation_status")), 0), r.get("policy_score") if r.get("policy_score") is not None else -999), reverse=True)
    for idx, row in enumerate(leaderboard, start=1):
        row["policy_rank"] = idx

    passed = [row for row in leaderboard if row.get("validation_status") == "pass"]
    watchlist = [row for row in leaderboard if row.get("validation_status") == "watchlist"]
    best = passed[0] if passed else (watchlist[0] if watchlist else (leaderboard[0] if leaderboard else None))
    report = {
        "status": "pass" if passed else "watchlist" if watchlist else "fail",
        "policy_gates": {
            "min_observations": settings.replay_policy_min_observations,
            "min_snapshots": settings.replay_policy_min_snapshots,
            "min_lift_vs_all": settings.replay_policy_min_lift_vs_all,
            "min_avg_end_return_pct": settings.replay_policy_min_avg_end_return_pct,
            "max_stop_rate": settings.replay_policy_max_stop_rate,
        },
        "baseline": {
            "all_observations": int(len(df)),
            "all_hit_rate": _safe_round(all_hit),
            "all_avg_end_return_pct": _safe_round(all_end),
            "shortlist_observations": int(len(shortlist_df)),
            "shortlist_hit_rate": _safe_round(shortlist_hit),
            "snapshot_count": snapshot_count,
        },
        "recommended_policy_count": len(passed),
        "watchlist_policy_count": len(watchlist),
        "recommended_policies": [row["policy_id"] for row in passed[:8]],
        "watchlist_policies": [row["policy_id"] for row in watchlist[:8]],
        "best_policy": best,
        "operator_guidance": [
            "Use the leaderboard as a historical gate for elite candidates, not as a probability table.",
            "Prefer policies marked pass; treat watchlist policies as research candidates only.",
            "Do not treat the whole top 20 as equivalent if only top-3/top-5 or score-threshold policies pass.",
        ],
        "note": "V2.3 validates elite policy gates over timing-only replay. It does not authorize calibrated live probabilities for the full composite score.",
    }
    return {"leaderboard": leaderboard, "report": report}

def _build_calibration_outputs(observations: pd.DataFrame, settings, replay_mode: str) -> Dict[str, Any]:
    if observations.empty:
        empty = []
        empty_diag = {
            "status": "failed",
            "reasons": ["No replay observations were produced."],
        }
        return {
            "score_band_metrics": empty,
            "calibration_table": empty,
            "top_vs_rest": empty,
            "quantile_lift_table": empty,
            "regime_slice_metrics": empty,
            "monotonicity_diagnostics": {
                "score_band_sequence": [],
                "score_band_hit_rates": [],
                "tolerance": settings.replay_monotonicity_tolerance,
                "violation_count": 0,
                "violations": [],
            },
            "discrimination_report": empty_diag,
            "surface_feature_report": {"status": "empty", "surface_label_distribution": {}, "surface_label_metrics": [], "surface_by_regime": [], "score_component_summary": {}},
            "elite_policy_leaderboard": [],
            "elite_policy_report": {"status": "failed", "reason": "No replay observations were produced.", "recommended_policy_count": 0, "best_policy": None},
            "replay_summary_extra": {
                "observation_count": 0,
                "eligible_for_probability_display": False,
                "eligibility_reason": "No replay observations were produced.",
                "parity_status": "failed",
                "discrimination_validation_status": "failed",
                "discrimination_validation_reason": "No replay observations were produced.",
            },
        }

    observations = observations.copy()
    observations["score_percentile"] = observations["score"].rank(pct=True, method="average") * 100
    observations["score_band"] = observations["score"].apply(_score_band)

    score_band_metrics = []
    for band, df in observations.groupby("score_band"):
        hit_rate = float(df["target_hit"].mean()) if len(df) else 0.0
        score_band_metrics.append({
            "score_band": band,
            "observations": int(len(df)),
            "hit_rate": round(hit_rate, 4),
            "avg_score": _safe_round(df["score"].mean(), 2),
            "avg_raw_score": _safe_round(df["raw_score"].mean(), 2) if "raw_score" in df.columns else None,
            "avg_context_adjustment": _safe_round(df["context_adjustment"].mean(), 2) if "context_adjustment" in df.columns else None,
            "avg_end_return_pct": _safe_round(df["end_return_pct"].mean()),
            "avg_max_return_pct": _safe_round(df["max_return_pct"].mean()),
            "avg_min_return_pct": _safe_round(df["min_return_pct"].mean()),
        })
    score_band_metrics = sorted(score_band_metrics, key=lambda row: _sort_score_band_key(row["score_band"]))

    calibration_rows = []
    for _, row in observations.iterrows():
        prob = observations[observations["score_band"] == row["score_band"]]["target_hit"].mean()
        calibration_rows.append({
            "score_band": row["score_band"],
            "score": float(row["score"]),
            "target_hit": int(row["target_hit"]),
            "predicted_probability": float(prob),
            "reliability_band": _reliability_bin(float(prob)),
        })
    calibration_df = pd.DataFrame(calibration_rows)
    calibration_table = []
    if not calibration_df.empty:
        for rel_band, df in calibration_df.groupby("reliability_band"):
            obs_rate = float(df["target_hit"].mean()) if len(df) else 0.0
            pred = float(df["predicted_probability"].mean()) if len(df) else 0.0
            calibration_table.append({
                "reliability_band": rel_band,
                "observations": int(len(df)),
                "avg_predicted_probability": round(pred, 4),
                "observed_hit_rate": round(obs_rate, 4),
                "calibration_gap": round(obs_rate - pred, 4),
            })
        calibration_table = sorted(calibration_table, key=lambda row: _sort_score_band_key(row["reliability_band"]))

    brier = float(((calibration_df["predicted_probability"] - calibration_df["target_hit"]) ** 2).mean()) if not calibration_df.empty else None
    pearson = _pearson_corr(observations["score"].tolist(), observations["target_hit"].tolist())
    spearman = _spearman_corr(observations["score"].tolist(), observations["target_hit"].tolist())
    top_vs_rest = _top_vs_rest_rows(observations)
    top_decile_row = next((row for row in top_vs_rest if row["bucket"] == "top_decile"), None)
    top_quintile_row = next((row for row in top_vs_rest if row["bucket"] == "top_quintile"), None)
    quantile_lift_table = _quantile_lift_rows(observations, bins=10)
    regime_slice_metrics = _regime_slice_rows(observations, settings.replay_min_regime_slice_observations)
    monotonicity = _monotonicity_diagnostics(score_band_metrics, settings.replay_monotonicity_tolerance)
    surface_feature_report = _build_surface_feature_report(observations)
    elite_policy_outputs = _build_elite_policy_outputs(observations, settings)
    elite_policy_leaderboard = elite_policy_outputs["leaderboard"]
    elite_policy_report = elite_policy_outputs["report"]

    enough_obs = len(observations) >= settings.calibration_min_observations
    enough_bands = all(row["observations"] >= settings.calibration_min_band_size for row in score_band_metrics if row["observations"] > 0)
    correlation_pass = spearman is not None and spearman >= settings.replay_monotonicity_min_correlation
    top_decile_lift = float(top_decile_row["lift_vs_all"]) if top_decile_row else None
    top_quintile_lift = float(top_quintile_row["lift_vs_all"]) if top_quintile_row else None
    top_decile_pass = top_decile_lift is not None and top_decile_lift >= settings.replay_min_top_decile_lift
    top_quintile_pass = top_quintile_lift is not None and top_quintile_lift >= settings.replay_min_top_quintile_lift
    monotonicity_pass = monotonicity["violation_count"] <= settings.replay_max_monotonicity_violations

    discrimination_reasons: List[str] = []
    if not enough_obs:
        discrimination_reasons.append(f"Observation count {len(observations)} is below minimum {settings.calibration_min_observations}.")
    if not enough_bands:
        discrimination_reasons.append("One or more score bands are too thin for stable band-level validation.")
    if not correlation_pass:
        discrimination_reasons.append(
            f"Spearman score/outcome correlation {spearman} is below minimum {settings.replay_monotonicity_min_correlation}."
        )
    if not top_decile_pass:
        discrimination_reasons.append(
            f"Top-decile lift {top_decile_lift} is below minimum {settings.replay_min_top_decile_lift}."
        )
    if not top_quintile_pass:
        discrimination_reasons.append(
            f"Top-quintile lift {top_quintile_lift} is below minimum {settings.replay_min_top_quintile_lift}."
        )
    if not monotonicity_pass:
        discrimination_reasons.append(
            f"Score-band monotonicity has {monotonicity['violation_count']} violations, above maximum {settings.replay_max_monotonicity_violations}."
        )

    discrimination_pass = all([enough_obs, enough_bands, correlation_pass, top_decile_pass, top_quintile_pass, monotonicity_pass])
    discrimination_report = {
        "status": "pass" if discrimination_pass else "fail",
        "gates": {
            "min_observations": {"passed": enough_obs, "value": int(len(observations)), "minimum": settings.calibration_min_observations},
            "min_band_size": {"passed": enough_bands, "minimum": settings.calibration_min_band_size},
            "spearman_correlation": {"passed": correlation_pass, "value": spearman, "minimum": settings.replay_monotonicity_min_correlation},
            "top_decile_lift": {"passed": top_decile_pass, "value": _safe_round(top_decile_lift), "minimum": settings.replay_min_top_decile_lift},
            "top_quintile_lift": {"passed": top_quintile_pass, "value": _safe_round(top_quintile_lift), "minimum": settings.replay_min_top_quintile_lift},
            "score_band_monotonicity": {"passed": monotonicity_pass, "violations": monotonicity["violation_count"], "maximum": settings.replay_max_monotonicity_violations},
        },
        "metrics": {
            "brier_score": _safe_round(brier),
            "pearson_score_outcome_correlation": pearson,
            "spearman_score_outcome_correlation": spearman,
            "top_decile_lift": _safe_round(top_decile_lift),
            "top_quintile_lift": _safe_round(top_quintile_lift),
            "score_band_violation_count": monotonicity["violation_count"],
        },
        "reasons": discrimination_reasons if discrimination_reasons else ["Timing-only replay passes the discrimination gates used in V2.1."],
        "note": "This report judges ranking discrimination only. It does not authorize live calibrated probability display for the full composite score.",
    }

    parity_status = "limited" if replay_mode == "timing_only" else "experimental"
    eligible = bool(enough_obs and enough_bands and replay_mode == "full_parity" and discrimination_pass)
    eligibility_reason = (
        "Probabilities remain disabled because replay mode is timing_only and does not reproduce point-in-time structural/catalyst inputs."
        if replay_mode == "timing_only"
        else "Probabilities remain disabled until full-parity replay is implemented and discrimination/calibration thresholds are met."
    )
    if eligible:
        eligibility_reason = "Replay achieved full parity and minimum calibration/discrimination thresholds."

    return {
        "score_band_metrics": score_band_metrics,
        "calibration_table": calibration_table,
        "top_vs_rest": top_vs_rest,
        "quantile_lift_table": quantile_lift_table,
        "regime_slice_metrics": regime_slice_metrics,
        "monotonicity_diagnostics": monotonicity,
        "discrimination_report": discrimination_report,
        "surface_feature_report": surface_feature_report,
        "elite_policy_leaderboard": elite_policy_leaderboard,
        "elite_policy_report": elite_policy_report,
        "replay_summary_extra": {
            "observation_count": int(len(observations)),
            "brier_score": _safe_round(brier),
            "score_outcome_correlation": pearson,
            "score_outcome_spearman": spearman,
            "top_decile_lift": _safe_round(top_decile_lift),
            "top_quintile_lift": _safe_round(top_quintile_lift),
            "score_band_monotonicity_violations": monotonicity["violation_count"],
            "eligible_for_probability_display": eligible,
            "eligibility_reason": eligibility_reason,
            "parity_status": parity_status,
            "discrimination_validation_status": discrimination_report["status"],
            "discrimination_validation_reason": " | ".join(discrimination_report["reasons"][:3]),
            "calibration_min_observations": settings.calibration_min_observations,
            "calibration_min_band_size": settings.calibration_min_band_size,
            "surface_label_distribution": surface_feature_report.get("surface_label_distribution", {}),
            "avg_surface_score": surface_feature_report.get("score_component_summary", {}).get("avg_surface_score"),
            "elite_policy_validation_status": elite_policy_report.get("status"),
            "elite_policy_recommended_count": elite_policy_report.get("recommended_policy_count"),
            "elite_policy_watchlist_count": elite_policy_report.get("watchlist_policy_count"),
            "best_elite_policy_id": (elite_policy_report.get("best_policy") or {}).get("policy_id"),
            "best_elite_policy_label": (elite_policy_report.get("best_policy") or {}).get("label"),
        },
    }



def run_replay_now(request_key: Optional[str] = None) -> str:
    global LAST_REPLAY_STARTED_AT, LAST_REPLAY_IDEMPOTENCY_KEY
    settings = load_settings()
    now = time.time()
    with REPLAY_STATUS_LOCK:
        active_replay_id = REPLAY_STATUS.get("replay_id")
        if REPLAY_STATUS.get("is_running"):
            if request_key and request_key == LAST_REPLAY_IDEMPOTENCY_KEY and active_replay_id:
                return str(active_replay_id)
            raise ReplayAlreadyRunningError("A replay is already in progress.")
        if now - LAST_REPLAY_STARTED_AT < REPLAY_COOLDOWN_SECONDS:
            if request_key and request_key == LAST_REPLAY_IDEMPOTENCY_KEY and active_replay_id:
                return str(active_replay_id)
            raise ReplayCooldownError("A replay was started very recently. Please wait a few seconds and try again.")
        replay_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        LAST_REPLAY_STARTED_AT = now
        LAST_REPLAY_IDEMPOTENCY_KEY = request_key
        REPLAY_STATUS.update({
            "is_running": True,
            "replay_id": replay_id,
            "phase": "starting",
            "message": "Preparing replay",
            "progress_current": 0,
            "progress_total": 1,
            "updated_at": utc_now_iso(),
        })
    threading.Thread(target=_run_replay_thread, args=(replay_id,), daemon=True).start()
    return replay_id



def _run_replay_thread(replay_id: str) -> None:
    settings = load_settings()
    replay_mode = settings.replay_default_mode
    provider = get_provider(settings.default_provider, settings.demo_mode, settings.max_workers)
    started_at = utc_now_iso()
    run_dir = ensure_dir(Path(settings.artifacts_dir) / f"replay_{replay_id}")
    warnings: List[str] = []
    log_lines: List[str] = [f"[{started_at}] Starting replay {replay_id}"]
    _update_replay_status(is_running=True, replay_id=replay_id, phase="starting", message="Preparing replay", progress_current=0, progress_total=1)
    upsert_replay_run(_build_replay_record(replay_id, settings, "running", started_at, message="Preparing replay", phase="starting", artifacts_dir=str(run_dir), replay_mode=replay_mode))

    try:
        universe_rows = load_universe()
        universe_rows = [{**row, "symbol": _normalize_ticker(row["symbol"]), "sector": normalize_sector_name(row.get("sector", ""))} for row in universe_rows if row.get("symbol")]
        if not universe_rows:
            raise RuntimeError("Universe loading failed; no symbols available for replay.")
        universe_rows = universe_rows[: settings.replay_ticker_limit]
        benchmark_tickers = ["SPY"]
        seen_sector_etfs: List[str] = []
        for row in universe_rows:
            etf = SECTOR_ETF_MAP.get(normalize_sector_name(row.get("sector", "")))
            if etf and etf not in seen_sector_etfs:
                seen_sector_etfs.append(etf)
        bulk_tickers = [row["symbol"] for row in universe_rows] + benchmark_tickers + seen_sector_etfs

        _update_replay_status(phase="fetching_history", message="Fetching replay history", progress_current=0, progress_total=len(bulk_tickers))
        history_map = provider.fetch_bulk_price_history(bulk_tickers, settings.replay_history_days)
        if "SPY" not in history_map or history_map["SPY"].empty:
            raise RuntimeError("SPY history unavailable for replay.")
        spy_history = history_map["SPY"].dropna(subset=["Close"]).sort_index()
        if len(spy_history) <= settings.replay_warmup_days + settings.outcome_horizon_days + 5:
            raise RuntimeError("Not enough benchmark history to build replay snapshots.")

        snapshot_dates = list(spy_history.index[settings.replay_warmup_days : len(spy_history) - settings.outcome_horizon_days])
        step = max(settings.replay_step_days, 1)
        snapshot_dates = snapshot_dates[::step]
        if len(snapshot_dates) > settings.replay_max_snapshots:
            snapshot_dates = snapshot_dates[-settings.replay_max_snapshots :]
        if not snapshot_dates:
            raise RuntimeError("No replay snapshot dates available after warmup/horizon filters.")

        observations: List[Dict[str, Any]] = []
        _update_replay_status(phase="replaying", message="Scoring historical snapshots", progress_current=0, progress_total=len(snapshot_dates))
        for snap_idx, snapshot_date in enumerate(snapshot_dates, start=1):
            snapshot_rows: List[Dict[str, Any]] = []
            spy_hist = spy_history[spy_history.index <= snapshot_date].copy()
            regime_metrics = _benchmark_regime_metrics(spy_hist)
            for row in universe_rows:
                ticker = row["symbol"]
                frame = history_map.get(ticker)
                if frame is None or frame.empty:
                    continue
                hist = frame[frame.index <= snapshot_date].copy()
                if len(hist) < max(60, settings.replay_warmup_days // 2):
                    continue
                sector_etf = SECTOR_ETF_MAP.get(normalize_sector_name(row.get("sector", "")))
                sector_hist = None
                if sector_etf and history_map.get(sector_etf) is not None:
                    sector_hist = history_map[sector_etf]
                    sector_hist = sector_hist[sector_hist.index <= snapshot_date].copy()
                metrics = compute_timing_metrics(hist, spy_hist, sector_hist)
                raw_score, reasons, risks, summary = score_timing(metrics)
                if math.isnan(raw_score):
                    continue
                context_adjustment, adj_reasons, adj_risks = _context_adjustment(metrics, regime_metrics)
                surface_metrics = _surface_feature_metrics(hist, spy_hist, sector_hist)
                continuation_score, continuation_reasons, continuation_risks = _score_continuation_surface(metrics, surface_metrics, regime_metrics)
                rebound_score, rebound_reasons, rebound_risks = _score_rebound_surface(metrics, surface_metrics, regime_metrics)
                score, surface_score, surface_label = _blend_surface_score_v22(float(raw_score), float(continuation_score), float(rebound_score), float(context_adjustment), regime_metrics)
                surface_reasons = continuation_reasons if surface_label == "continuation" else rebound_reasons if surface_label == "rebound" else (continuation_reasons[:2] + rebound_reasons[:2])
                surface_risks = continuation_risks if surface_label == "continuation" else rebound_risks if surface_label == "rebound" else (continuation_risks[:2] + rebound_risks[:2])
                entry_price = float(hist["Close"].iloc[-1])
                outcome = _evaluate_forward_outcome(frame, snapshot_date, entry_price, settings.outcome_target_up_pct, settings.outcome_stop_down_pct, settings.outcome_horizon_days)
                snapshot_rows.append({
                    "snapshot_date": str(pd.Timestamp(snapshot_date).date()),
                    "ticker": ticker,
                    "company_name": row.get("name") or ticker,
                    "sector": normalize_sector_name(row.get("sector", "")),
                    "market_regime": regime_metrics.get("regime_label"),
                    "benchmark_one_month_return": regime_metrics.get("benchmark_one_month_return"),
                    "benchmark_three_month_return": regime_metrics.get("benchmark_three_month_return"),
                    "benchmark_rsi14": regime_metrics.get("benchmark_rsi14"),
                    "benchmark_volatility20": regime_metrics.get("benchmark_volatility20"),
                    "raw_score": round(float(raw_score), 2),
                    "context_adjustment": context_adjustment,
                    "surface_score": surface_score,
                    "continuation_score": continuation_score,
                    "rebound_score": rebound_score,
                    "surface_label": surface_label,
                    "score": score,
                    "entry_price": round(entry_price, 4),
                    "technical_summary": f"{summary}; surface {surface_label}",
                    "reason_codes": " | ".join((reasons + adj_reasons + surface_reasons)[:6]) if (reasons or adj_reasons or surface_reasons) else "No explicit timing reasons",
                    "risk_flags": " | ".join((risks + adj_risks + surface_risks)[:6]) if (risks or adj_risks or surface_risks) else "None identified",
                    **surface_metrics,
                    **outcome,
                })
            snapshot_rows = sorted(snapshot_rows, key=lambda item: item["score"], reverse=True)
            if len(snapshot_rows) < settings.replay_min_rows_per_snapshot:
                warnings.append(f"Skipped snapshot {pd.Timestamp(snapshot_date).date()} due to only {len(snapshot_rows)} scored rows.")
                _update_replay_status(phase="replaying", message=f"Skipped thin snapshot {pd.Timestamp(snapshot_date).date()}", progress_current=snap_idx, progress_total=len(snapshot_dates))
                continue
            for rank, item in enumerate(snapshot_rows, start=1):
                item["score_rank"] = rank
                item["snapshot_rank_pct"] = round((rank / len(snapshot_rows)) * 100, 2)
                item["shortlist_flag"] = 1 if rank <= settings.shortlist_size else 0
                item["target_hit"] = int(item.get("status") == "target_hit")
                item["stop_hit"] = int(item.get("status") == "stop_hit")
                item["expired_flag"] = int(item.get("status") == "expired")
                item["score_band"] = _score_band(float(item["score"]))
            observations.extend(snapshot_rows)
            _update_replay_status(phase="replaying", message=f"Processed snapshot {pd.Timestamp(snapshot_date).date()}", progress_current=snap_idx, progress_total=len(snapshot_dates))

        obs_df = pd.DataFrame(observations)
        calibration_outputs = _build_calibration_outputs(obs_df, settings, replay_mode)
        top_vs_rest = calibration_outputs["top_vs_rest"]
        score_band_metrics = calibration_outputs["score_band_metrics"]
        calibration_table = calibration_outputs["calibration_table"]
        quantile_lift_table = calibration_outputs["quantile_lift_table"]
        regime_slice_metrics = calibration_outputs["regime_slice_metrics"]
        monotonicity_diagnostics = calibration_outputs["monotonicity_diagnostics"]
        discrimination_report = calibration_outputs["discrimination_report"]
        surface_feature_report = calibration_outputs["surface_feature_report"]
        elite_policy_leaderboard = calibration_outputs["elite_policy_leaderboard"]
        elite_policy_report = calibration_outputs["elite_policy_report"]
        replay_extra = calibration_outputs["replay_summary_extra"]
        parity_assessment = {
            "replay_mode": replay_mode,
            "replay_surface": "elite_policy_validation_v2_3",
            "parity_status": replay_extra.get("parity_status"),
            "eligible_for_probability_display": replay_extra.get("eligible_for_probability_display"),
            "eligibility_reason": replay_extra.get("eligibility_reason"),
            "discrimination_validation_status": replay_extra.get("discrimination_validation_status"),
            "discrimination_validation_reason": replay_extra.get("discrimination_validation_reason"),
            "limitations": [
                "Replay still uses timing-only historical inputs because point-in-time fundamentals and news are not available from the current provider stack.",
                "V2.3 validates elite policy gates such as top-3/top-5 and score-threshold slices, but this is still not the full live composite score.",
                "Do not label live scanner scores as calibrated probabilities until full-parity replay exists and calibration thresholds are met.",
            ],
        }
        replay_summary = {
            "replay_id": replay_id,
            "started_at": started_at,
            "ended_at": utc_now_iso(),
            "provider": provider.provider_name,
            "replay_mode": replay_mode,
            "replay_surface": "elite_policy_validation_v2_3",
            "universe_size_loaded": len(universe_rows),
            "snapshot_count_requested": min(len(spy_history.index[settings.replay_warmup_days : len(spy_history) - settings.outcome_horizon_days][:: max(step,1)]), settings.replay_max_snapshots),
            "snapshot_count_completed": int(obs_df["snapshot_date"].nunique()) if not obs_df.empty else 0,
            "observation_count": int(len(obs_df)),
            "shortlist_observation_count": int(obs_df["shortlist_flag"].sum()) if not obs_df.empty else 0,
            "target_definition": {
                "target_up_pct": settings.outcome_target_up_pct,
                "stop_down_pct": settings.outcome_stop_down_pct,
                "horizon_days": settings.outcome_horizon_days,
            },
            "validation": replay_extra,
            "warnings": warnings[:50],
            "build": {
                "app_version": settings.app_version,
                "build_id": settings.build_id,
                "build_timestamp_utc": settings.build_timestamp_utc,
                "artifact_schema_version": settings.artifact_schema_version,
            },
            "note": "This V2.3 build validates elite policy gates over the replay surface. It produces a policy leaderboard, not calibrated live probabilities. Full live probability display remains disabled until replay parity covers point-in-time fundamentals and catalysts.",
        }

        manifest = {
            "replay_id": replay_id,
            "required_artifacts": sorted(REQUIRED_REPLAY_ARTIFACTS),
            "replay_mode": replay_mode,
            "replay_surface": "elite_policy_validation_v2_3",
            "build": replay_summary["build"],
        }
        write_json(run_dir / "replay_summary.json", replay_summary)
        write_csv(run_dir / "score_band_metrics.csv", score_band_metrics)
        write_csv(run_dir / "calibration_table.csv", calibration_table)
        write_csv(run_dir / "candidate_outcomes.csv", [sanitize_row(row) for row in observations])
        write_csv(run_dir / "top_vs_rest_comparison.csv", top_vs_rest)
        write_csv(run_dir / "quantile_lift_table.csv", quantile_lift_table)
        write_csv(run_dir / "regime_slice_metrics.csv", regime_slice_metrics)
        write_json(run_dir / "discrimination_report.json", discrimination_report)
        write_json(run_dir / "monotonicity_diagnostics.json", monotonicity_diagnostics)
        write_json(run_dir / "surface_feature_report.json", surface_feature_report)
        write_csv(run_dir / "elite_policy_leaderboard.csv", elite_policy_leaderboard)
        write_json(run_dir / "elite_policy_report.json", elite_policy_report)
        write_text(run_dir / "validation_log.txt", "\n".join(log_lines + [f"[{utc_now_iso()}] Replay completed successfully"]))
        write_json(run_dir / "config_used.json", {
            "replay_ticker_limit": settings.replay_ticker_limit,
            "replay_max_snapshots": settings.replay_max_snapshots,
            "replay_history_days": settings.replay_history_days,
            "replay_warmup_days": settings.replay_warmup_days,
            "replay_step_days": settings.replay_step_days,
            "replay_min_rows_per_snapshot": settings.replay_min_rows_per_snapshot,
            "replay_default_mode": settings.replay_default_mode,
            "outcome_target_up_pct": settings.outcome_target_up_pct,
            "outcome_stop_down_pct": settings.outcome_stop_down_pct,
            "outcome_horizon_days": settings.outcome_horizon_days,
            "replay_monotonicity_min_correlation": settings.replay_monotonicity_min_correlation,
            "replay_min_top_decile_lift": settings.replay_min_top_decile_lift,
            "replay_min_top_quintile_lift": settings.replay_min_top_quintile_lift,
            "replay_max_monotonicity_violations": settings.replay_max_monotonicity_violations,
            "replay_monotonicity_tolerance": settings.replay_monotonicity_tolerance,
            "replay_policy_min_observations": settings.replay_policy_min_observations,
            "replay_policy_min_snapshots": settings.replay_policy_min_snapshots,
            "replay_policy_min_lift_vs_all": settings.replay_policy_min_lift_vs_all,
            "replay_policy_min_avg_end_return_pct": settings.replay_policy_min_avg_end_return_pct,
            "replay_policy_max_stop_rate": settings.replay_policy_max_stop_rate,
        })
        write_json(run_dir / "replay_parity_assessment.json", parity_assessment)
        write_json(run_dir / "replay_artifact_manifest.json", manifest)
        manifest["artifacts_present_before_zip"] = sorted(p.name for p in run_dir.iterdir() if p.is_file())
        missing = sorted(REQUIRED_REPLAY_ARTIFACTS - set(manifest["artifacts_present_before_zip"]))
        integrity = {
            "status": "pass" if not missing else "fail",
            "missing_artifacts": missing,
            "checked_at": utc_now_iso(),
        }
        manifest["artifact_integrity"] = integrity
        write_json(run_dir / "replay_artifact_manifest.json", manifest)
        if integrity["status"] != "pass":
            raise RuntimeError("Replay artifact integrity failed: " + ", ".join(missing))
        zip_path = run_dir / f"{replay_id}_validation_pack.zip"
        zip_directory(run_dir, zip_path)
        ended_at = utc_now_iso()
        replay_summary["artifact_integrity"] = integrity
        write_json(run_dir / "replay_summary.json", replay_summary)
        upsert_replay_run(_build_replay_record(replay_id, settings, "completed", started_at, message="Replay completed", ended_at=ended_at, progress_current=len(snapshot_dates), progress_total=len(snapshot_dates), phase="completed", warnings=warnings, artifacts_dir=str(run_dir), artifact_zip_path=str(zip_path), summary=replay_summary, replay_mode=replay_mode))
        _update_replay_status(is_running=False, replay_id=replay_id, phase="completed", message="Replay completed", progress_current=len(snapshot_dates), progress_total=len(snapshot_dates))
    except Exception as exc:
        log_lines.append(f"[{utc_now_iso()}] FAILED: {exc}")
        write_text(run_dir / "validation_log.txt", "\n".join(log_lines))
        ended_at = utc_now_iso()
        upsert_replay_run(_build_replay_record(replay_id, settings, "failed", started_at, message=str(exc), ended_at=ended_at, phase="failed", warnings=warnings + [str(exc)], artifacts_dir=str(run_dir), replay_mode=replay_mode))
        _update_replay_status(is_running=False, replay_id=replay_id, phase="failed", message=str(exc), progress_current=0, progress_total=0)



def latest_replay_payload() -> Optional[Dict[str, Any]]:
    run = deserialize_replay_run(get_latest_replay_run())
    if not run:
        return None
    artifacts_dir = Path(run.get("artifacts_dir") or "")
    replay_summary = dict(run.get("summary") or {})
    validation = dict(replay_summary.get("validation") or {})
    replay_summary["validation"] = {
        "parity_status": validation.get("parity_status", "unknown"),
        "discrimination_validation_status": validation.get("discrimination_validation_status", "unknown"),
        "score_outcome_spearman": validation.get("score_outcome_spearman"),
        "top_decile_lift": validation.get("top_decile_lift"),
        "score_band_monotonicity_violations": validation.get("score_band_monotonicity_violations"),
        "discrimination_validation_reason": validation.get("discrimination_validation_reason", "No replay discrimination decision available yet."),
        "eligible_for_probability_display": validation.get("eligible_for_probability_display", False),
        "eligibility_reason": validation.get("eligibility_reason", "Probability display remains disabled until replay evidence is available."),
        "elite_policy_validation_status": validation.get("elite_policy_validation_status", "unknown"),
        "elite_policy_recommended_count": validation.get("elite_policy_recommended_count", 0),
        "elite_policy_watchlist_count": validation.get("elite_policy_watchlist_count", 0),
        "best_elite_policy_id": validation.get("best_elite_policy_id"),
        "best_elite_policy_label": validation.get("best_elite_policy_label"),
    }
    calibration_table: List[Dict[str, Any]] = []
    score_band_metrics: List[Dict[str, Any]] = []
    top_vs_rest: List[Dict[str, Any]] = []
    quantile_lift_table: List[Dict[str, Any]] = []
    regime_slice_metrics: List[Dict[str, Any]] = []
    elite_policy_leaderboard: List[Dict[str, Any]] = []
    elite_policy_report: Dict[str, Any] = {"status": "unknown", "recommended_policy_count": 0, "best_policy": None, "policy_gates": {}}
    discrimination_report: Dict[str, Any] = {"gates": {}, "summary": {}}
    monotonicity_diagnostics: Dict[str, Any] = {"score_band_hit_rates": [], "violations": []}
    if artifacts_dir.exists():
        csv_targets = {
            "calibration_table.csv": "calibration_table",
            "score_band_metrics.csv": "score_band_metrics",
            "top_vs_rest_comparison.csv": "top_vs_rest",
            "quantile_lift_table.csv": "quantile_lift_table",
            "regime_slice_metrics.csv": "regime_slice_metrics",
            "elite_policy_leaderboard.csv": "elite_policy_leaderboard",
        }
        for name, attr in csv_targets.items():
            path = artifacts_dir / name
            if path.exists():
                try:
                    rows = pd.read_csv(path).to_dict(orient="records")
                    if attr == "calibration_table":
                        calibration_table = rows
                    elif attr == "score_band_metrics":
                        score_band_metrics = rows
                    elif attr == "top_vs_rest":
                        top_vs_rest = rows
                    elif attr == "quantile_lift_table":
                        quantile_lift_table = rows
                    elif attr == "elite_policy_leaderboard":
                        elite_policy_leaderboard = rows
                    else:
                        regime_slice_metrics = rows
                except Exception:
                    pass
        json_targets = {
            "discrimination_report.json": "discrimination_report",
            "monotonicity_diagnostics.json": "monotonicity_diagnostics",
            "elite_policy_report.json": "elite_policy_report",
        }
        for name, attr in json_targets.items():
            path = artifacts_dir / name
            if path.exists():
                try:
                    payload = json.loads(path.read_text())
                    if attr == "discrimination_report":
                        discrimination_report = payload if isinstance(payload, dict) else {"gates": {}, "summary": {}}
                    elif attr == "elite_policy_report":
                        elite_policy_report = payload if isinstance(payload, dict) else {"status": "unknown", "recommended_policy_count": 0, "best_policy": None, "policy_gates": {}}
                    else:
                        monotonicity_diagnostics = payload if isinstance(payload, dict) else {"score_band_hit_rates": [], "violations": []}
                except Exception:
                    pass
    discrimination_report.setdefault("gates", {})
    discrimination_report.setdefault("summary", {})
    monotonicity_diagnostics.setdefault("score_band_hit_rates", [])
    monotonicity_diagnostics.setdefault("violations", [])
    elite_policy_report.setdefault("status", "unknown")
    elite_policy_report.setdefault("recommended_policy_count", 0)
    elite_policy_report.setdefault("watchlist_policy_count", 0)
    elite_policy_report.setdefault("policy_gates", {})
    required_names = set(REQUIRED_REPLAY_ARTIFACTS)
    existing_names = {p.name for p in artifacts_dir.iterdir() if p.is_file()} if artifacts_dir.exists() else set()
    missing_required_artifacts = sorted(required_names - existing_names)
    run["summary"] = replay_summary
    run["calibration_table"] = calibration_table
    run["score_band_metrics"] = score_band_metrics
    run["top_vs_rest"] = top_vs_rest
    run["quantile_lift_table"] = quantile_lift_table
    run["regime_slice_metrics"] = regime_slice_metrics
    run["elite_policy_leaderboard"] = elite_policy_leaderboard
    run["elite_policy_report"] = elite_policy_report
    run["discrimination_report"] = discrimination_report
    run["monotonicity_diagnostics"] = monotonicity_diagnostics
    run["can_download_validation_pack"] = bool(run.get("status") == "completed" and not missing_required_artifacts and run.get("artifact_zip_path"))
    run["missing_required_artifacts"] = missing_required_artifacts
    run["has_validation_log"] = bool(artifacts_dir.exists() and (artifacts_dir / "validation_log.txt").exists())
    return run
