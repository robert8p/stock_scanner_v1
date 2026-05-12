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
                score = round(max(0.0, min(100.0, raw_score + context_adjustment)), 2)
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
                    "score": score,
                    "entry_price": round(entry_price, 4),
                    "technical_summary": summary,
                    "reason_codes": " | ".join((reasons + adj_reasons)[:5]) if (reasons or adj_reasons) else "No explicit timing reasons",
                    "risk_flags": " | ".join((risks + adj_risks)[:5]) if (risks or adj_risks) else "None identified",
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
        replay_extra = calibration_outputs["replay_summary_extra"]
        parity_assessment = {
            "replay_mode": replay_mode,
            "replay_surface": "context_adjusted_timing_v2_1",
            "parity_status": replay_extra.get("parity_status"),
            "eligible_for_probability_display": replay_extra.get("eligible_for_probability_display"),
            "eligibility_reason": replay_extra.get("eligibility_reason"),
            "discrimination_validation_status": replay_extra.get("discrimination_validation_status"),
            "discrimination_validation_reason": replay_extra.get("discrimination_validation_reason"),
            "limitations": [
                "Replay still uses timing-only historical inputs because point-in-time fundamentals and news are not available from the current provider stack.",
                "V2.1 adds context-adjusted timing replay and stronger validation gates, but this is still not the full live composite score.",
                "Do not label live scanner scores as calibrated probabilities until full-parity replay exists and calibration thresholds are met.",
            ],
        }
        replay_summary = {
            "replay_id": replay_id,
            "started_at": started_at,
            "ended_at": utc_now_iso(),
            "provider": provider.provider_name,
            "replay_mode": replay_mode,
            "replay_surface": "context_adjusted_timing_v2_1",
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
            "note": "This V2.1 build hardens replay discrimination with context-adjusted timing replay, regime slicing, and monotonicity gates. Full live probability display remains disabled until replay parity covers point-in-time fundamentals and catalysts.",
        }

        manifest = {
            "replay_id": replay_id,
            "required_artifacts": sorted(REQUIRED_REPLAY_ARTIFACTS),
            "replay_mode": replay_mode,
            "replay_surface": "context_adjusted_timing_v2_1",
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
    }
    calibration_table: List[Dict[str, Any]] = []
    score_band_metrics: List[Dict[str, Any]] = []
    top_vs_rest: List[Dict[str, Any]] = []
    quantile_lift_table: List[Dict[str, Any]] = []
    regime_slice_metrics: List[Dict[str, Any]] = []
    discrimination_report: Dict[str, Any] = {"gates": {}, "summary": {}}
    monotonicity_diagnostics: Dict[str, Any] = {"score_band_hit_rates": [], "violations": []}
    if artifacts_dir.exists():
        csv_targets = {
            "calibration_table.csv": "calibration_table",
            "score_band_metrics.csv": "score_band_metrics",
            "top_vs_rest_comparison.csv": "top_vs_rest",
            "quantile_lift_table.csv": "quantile_lift_table",
            "regime_slice_metrics.csv": "regime_slice_metrics",
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
                    else:
                        regime_slice_metrics = rows
                except Exception:
                    pass
        json_targets = {
            "discrimination_report.json": "discrimination_report",
            "monotonicity_diagnostics.json": "monotonicity_diagnostics",
        }
        for name, attr in json_targets.items():
            path = artifacts_dir / name
            if path.exists():
                try:
                    payload = json.loads(path.read_text())
                    if attr == "discrimination_report":
                        discrimination_report = payload if isinstance(payload, dict) else {"gates": {}, "summary": {}}
                    else:
                        monotonicity_diagnostics = payload if isinstance(payload, dict) else {"score_band_hit_rates": [], "violations": []}
                except Exception:
                    pass
    discrimination_report.setdefault("gates", {})
    discrimination_report.setdefault("summary", {})
    monotonicity_diagnostics.setdefault("score_band_hit_rates", [])
    monotonicity_diagnostics.setdefault("violations", [])
    run["summary"] = replay_summary
    run["calibration_table"] = calibration_table
    run["score_band_metrics"] = score_band_metrics
    run["top_vs_rest"] = top_vs_rest
    run["quantile_lift_table"] = quantile_lift_table
    run["regime_slice_metrics"] = regime_slice_metrics
    run["discrimination_report"] = discrimination_report
    run["monotonicity_diagnostics"] = monotonicity_diagnostics
    return run
