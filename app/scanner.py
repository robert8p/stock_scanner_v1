from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd

from .config import load_settings
from .db import deserialize_candidate, deserialize_run, init_db, list_candidates, list_runs, replace_candidates, upsert_run, upsert_shortlist_outcomes, list_shortlist_outcomes, summarize_shortlist_outcomes
from .providers import get_provider
from .providers.base import TickerDataBundle
from .scoring import SECTOR_ETF_MAP, compute_timing_metrics, confidence_band, normalize_sector_name, score_catalyst, score_structural, score_timing
from .storage import ensure_dir, sanitize_row, utc_now_iso, write_csv, write_json, write_text, zip_directory
from .universe import load_universe


STATUS_LOCK = threading.Lock()
SCAN_STATUS: Dict[str, Any] = {
    "is_running": False,
    "run_id": None,
    "phase": "idle",
    "message": "Ready",
    "progress_current": 0,
    "progress_total": 0,
    "updated_at": utc_now_iso(),
}
LAST_SCAN_STARTED_AT = 0.0
LAST_IDEMPOTENCY_KEY: Optional[str] = None


class ScanAlreadyRunningError(RuntimeError):
    pass


class ScanCooldownError(RuntimeError):
    pass


def sanitize_settings_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    sensitive = {"finnhub_api_key", "polygon_api_key", "alpaca_api_key", "alpaca_api_secret"}
    return {key: ("***REDACTED***" if key in sensitive and value else value) for key, value in payload.items()}


def _persist_runtime_status(status: Dict[str, Any]) -> None:
    settings = load_settings()
    try:
        Path(settings.runtime_status_path).write_text(json.dumps(status, indent=2))
    except Exception:
        pass


def _sanitize_run_for_status(run: Dict[str, Any]) -> Dict[str, Any]:
    safe = dict(run)
    if "settings_json" in safe and isinstance(safe["settings_json"], str):
        try:
            safe["settings"] = sanitize_settings_payload(json.loads(safe["settings_json"]))
        except Exception:
            pass
    if "settings" in safe and isinstance(safe["settings"], dict):
        safe["settings"] = sanitize_settings_payload(safe["settings"])
    return safe


def get_runtime_status() -> Dict[str, Any]:
    init_db()
    settings = load_settings()
    with STATUS_LOCK:
        status = dict(SCAN_STATUS)
    status["build"] = {
        "app_version": settings.app_version,
        "build_id": settings.build_id,
        "build_timestamp_utc": settings.build_timestamp_utc,
        "artifact_schema_version": settings.artifact_schema_version,
    }
    status["outcome_tracking_summary"] = summarize_shortlist_outcomes()
    run_history = list_runs(limit=1)
    if run_history:
        latest = deserialize_run(run_history[0])
        if latest:
            status["latest_run"] = _sanitize_run_for_status(latest)
    return status


def _update_status(**kwargs: Any) -> None:
    with STATUS_LOCK:
        SCAN_STATUS.update(kwargs)
        SCAN_STATUS["updated_at"] = utc_now_iso()
        status = dict(SCAN_STATUS)
    _persist_runtime_status(status)


def _initial_prefilter(history_map: Dict[str, pd.DataFrame], universe_rows: List[Dict[str, str]], settings) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    benchmark_spy = history_map.get("SPY")
    benchmark_set = {"SPY"} | set(SECTOR_ETF_MAP.values())
    scored = []
    universe_by_ticker = {row["symbol"]: row for row in universe_rows}
    missing_history = []
    for ticker, row in universe_by_ticker.items():
        if ticker in benchmark_set:
            continue
        frame = history_map.get(ticker)
        if frame is None or frame.empty:
            missing_history.append(ticker)
            continue
        sector = normalize_sector_name(row.get("sector", ""))
        sector_etf = SECTOR_ETF_MAP.get(sector)
        sector_history = history_map.get(sector_etf) if sector_etf else None
        metrics = compute_timing_metrics(frame, benchmark_spy, sector_history)
        score, _, _, _ = score_timing(metrics)
        scored.append({"ticker": ticker, "score": score})
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    keep = {row["ticker"] for row in ranked[: settings.enrichment_limit]}
    diagnostics = {
        "price_history_available_count": len(scored),
        "price_history_missing_count": len(missing_history),
        "prefilter_ranked_count": len(ranked),
        "prefilter_keep_count": len(keep),
        "missing_price_history_examples": missing_history[:10],
    }
    return [row for row in universe_rows if row["symbol"] in keep], diagnostics


def _normalize_ticker(symbol: str) -> str:
    return symbol.replace(".", "-").upper()


def _company_dedupe_key(row: Dict[str, Any]) -> str:
    cik = str(row.get("cik") or "").strip()
    if cik:
        return f"cik:{cik}"
    company_name = (row.get("company_name") or "").lower()
    company_name = re.sub(r"\(class [^)]+\)", "", company_name)
    company_name = re.sub(r"[^a-z0-9]+", " ", company_name)
    company_name = re.sub(r"\b(inc|corp|corporation|company|co|holdings|group|plc|class)\b", "", company_name)
    company_name = re.sub(r"\s+", " ", company_name).strip()
    return f"name:{company_name}" if company_name else f"ticker:{row.get('ticker','')}"


def _dedupe_share_classes(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[str]]:
    kept: Dict[str, Dict[str, Any]] = {}
    removed_messages: List[str] = []
    for row in rows:
        key = _company_dedupe_key(row)
        existing = kept.get(key)
        if existing is None or row["overall_score"] > existing["overall_score"]:
            if existing is not None:
                removed_messages.append(f"Removed duplicate share class {existing['ticker']} in favor of {row['ticker']}")
            kept[key] = row
        else:
            removed_messages.append(f"Removed duplicate share class {row['ticker']} in favor of {existing['ticker']}")
    return list(kept.values()), removed_messages


def _build_run_record(run_id: str, settings, status: str, started_at: str, message: str = "", **extra: Any) -> Dict[str, Any]:
    record = {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": extra.get("ended_at"),
        "status": status,
        "progress_current": extra.get("progress_current", 0),
        "progress_total": extra.get("progress_total", 0),
        "phase": extra.get("phase", ""),
        "message": message,
        "universe_name": settings.default_universe_name,
        "universe_size": extra.get("universe_size", 0),
        "enrichment_size": extra.get("enrichment_size", 0),
        "shortlist_size": extra.get("shortlist_size", 0),
        "provider": "demo" if settings.demo_mode else settings.default_provider,
        "settings_json": json.dumps(sanitize_settings_payload(settings.to_dict()), default=str),
        "warnings_json": json.dumps(extra.get("warnings", []), default=str),
        "artifacts_dir": extra.get("artifacts_dir", ""),
        "artifact_zip_path": extra.get("artifact_zip_path", ""),
        "summary_json": json.dumps(extra.get("summary", {}), default=str),
    }
    return record


def _coverage_counts(feature_rows: List[Dict[str, Any]], columns: List[str]) -> Dict[str, Any]:
    total = len(feature_rows)
    payload: Dict[str, Any] = {}
    for column in columns:
        present = 0
        for row in feature_rows:
            value = row.get(column)
            if value is None or value == "":
                continue
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                continue
            present += 1
        payload[column] = {
            "present_count": present,
            "missing_count": max(total - present, 0),
            "coverage_pct": round((present / total) * 100, 1) if total else 0.0,
        }
    return payload



def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, pd.Timestamp):
        dt = value.to_pydatetime()
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _frame_last_timestamp(frame: Optional[pd.DataFrame]) -> Optional[str]:
    if frame is None or frame.empty:
        return None
    try:
        ts = frame.index[-1]
    except Exception:
        return None
    parsed = _parse_dt(ts)
    return parsed.isoformat() if parsed else None


def _latest_news_timestamp(news_rows: List[Dict[str, Any]]) -> Optional[str]:
    timestamps = [_parse_dt(item.get('published_at')) for item in news_rows if item.get('published_at')]
    timestamps = [item for item in timestamps if item]
    if not timestamps:
        return None
    return max(timestamps).isoformat()


def _evidence_quality(bundle: TickerDataBundle, timing_metrics: Dict[str, Any], prepared_news: List[Dict[str, Any]], settings) -> Dict[str, Any]:
    required_fundamentals = [
        bundle.fundamentals.get('revenueGrowth'),
        bundle.fundamentals.get('earningsGrowth'),
        bundle.fundamentals.get('profitMargins'),
        bundle.fundamentals.get('operatingMargins'),
        bundle.fundamentals.get('debtToEquity'),
        bundle.fundamentals.get('currentRatio'),
        bundle.fundamentals.get('returnOnEquity'),
        bundle.fundamentals.get('forwardPE'),
        bundle.fundamentals.get('freeCashflowYield'),
    ]
    required_timing = [
        timing_metrics.get('relative_strength_vs_spy'),
        timing_metrics.get('relative_strength_vs_sector'),
        timing_metrics.get('trend_strength'),
        timing_metrics.get('momentum_63d'),
    ]
    values = required_fundamentals + required_timing
    present = 0
    for value in values:
        if value is None or value == '':
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            continue
        present += 1
    completeness_pct = round((present / len(values)) * 100, 1) if values else 0.0

    price_last_ts = _frame_last_timestamp(bundle.price_history)
    news_last_ts = _latest_news_timestamp(prepared_news)
    now = datetime.now(timezone.utc)
    price_age_days = None
    news_age_days = None
    if price_last_ts:
        dt = _parse_dt(price_last_ts)
        if dt:
            price_age_days = round((now - dt).total_seconds() / 86400, 2)
    if news_last_ts:
        dt = _parse_dt(news_last_ts)
        if dt:
            news_age_days = round((now - dt).total_seconds() / 86400, 2)

    staleness_flags = []
    if price_age_days is None or price_age_days > settings.stale_price_max_age_days:
        staleness_flags.append('Price data stale')
    if news_age_days is None or news_age_days > settings.stale_news_max_age_days:
        staleness_flags.append('News data stale')
    if completeness_pct < settings.min_core_feature_coverage_pct:
        staleness_flags.append('Core feature coverage thin')

    if completeness_pct >= 85 and not staleness_flags:
        label = 'High'
    elif completeness_pct >= settings.min_core_feature_coverage_pct and len(staleness_flags) <= 1:
        label = 'Moderate'
    else:
        label = 'Thin'

    return {
        'evidence_completeness_pct': completeness_pct,
        'data_quality_label': label,
        'price_last_timestamp': price_last_ts,
        'news_last_timestamp': news_last_ts,
        'price_age_days': price_age_days,
        'news_age_days': news_age_days,
        'staleness_flags': staleness_flags,
    }


def _run_degradation_assessment(feature_rows: List[Dict[str, Any]], coverage: Dict[str, Any], settings, price_history_available_count: int, universe_loaded: int) -> Dict[str, Any]:
    issues = []
    price_coverage_pct = round((price_history_available_count / universe_loaded) * 100, 1) if universe_loaded else 0.0
    if price_coverage_pct < settings.min_price_history_coverage_pct:
        issues.append(f'Price-history coverage {price_coverage_pct}% below threshold {settings.min_price_history_coverage_pct}%')

    core_feature_keys = [
        'fund_revenueGrowth', 'fund_earningsGrowth', 'fund_profitMargins', 'fund_operatingMargins',
        'fund_debtToEquity', 'fund_currentRatio', 'fund_returnOnEquity', 'fund_forwardPE',
        'fund_freeCashflowYield', 'timing_relative_strength_vs_spy', 'timing_relative_strength_vs_sector',
    ]
    core_coverages = [coverage.get(key, {}).get('coverage_pct', 0.0) for key in core_feature_keys if key in coverage]
    median_core_feature_coverage_pct = round(float(pd.Series(core_coverages).median()), 1) if core_coverages else 0.0
    if median_core_feature_coverage_pct < settings.min_core_feature_coverage_pct:
        issues.append(f'Median core-feature coverage {median_core_feature_coverage_pct}% below threshold {settings.min_core_feature_coverage_pct}%')

    price_ages = [row.get('price_age_days') for row in feature_rows if row.get('price_age_days') is not None]
    news_ages = [row.get('news_age_days') for row in feature_rows if row.get('news_age_days') is not None]
    median_price_age_days = round(float(pd.Series(price_ages).median()), 2) if price_ages else None
    median_news_age_days = round(float(pd.Series(news_ages).median()), 2) if news_ages else None
    if median_price_age_days is None or median_price_age_days > settings.stale_price_max_age_days:
        issues.append('Median price data age is stale')
    if median_news_age_days is None or median_news_age_days > settings.stale_news_max_age_days:
        issues.append('Median news age is stale')

    thin_count = sum(1 for row in feature_rows if row.get('data_quality_label') == 'Thin')
    degraded = bool(issues)
    return {
        'degraded': degraded,
        'issues': issues,
        'price_history_coverage_pct': price_coverage_pct,
        'median_core_feature_coverage_pct': median_core_feature_coverage_pct,
        'median_price_age_days': median_price_age_days,
        'median_news_age_days': median_news_age_days,
        'thin_evidence_count': thin_count,
        'thin_evidence_pct': round((thin_count / len(feature_rows)) * 100, 1) if feature_rows else 0.0,
    }


SORT_MODE_LABELS = {
    "score": "All ranked (score order)",
    "catalyst_first": "Catalyst-backed first",
    "quality_first": "Quality/momentum first",
}
SHORTLIST_DEFAULT_SORT_MODE = "catalyst_first"

SUPPORT_LEVEL_LABELS = {
    "backed": "Catalyst-backed",
    "supported": "Catalyst-supported",
    "mixed": "Quality/momentum · mixed catalyst",
    "weak": "Quality/momentum · weak catalyst",
}


def normalize_sort_mode(sort_mode: str | None) -> str:
    candidate = (sort_mode or "catalyst_first").strip().lower().replace("-", "_")
    return candidate if candidate in SORT_MODE_LABELS else SHORTLIST_DEFAULT_SORT_MODE


def _support_priority(sort_mode: str) -> Dict[str, int]:
    if sort_mode == "quality_first":
        return {"mixed": 0, "weak": 1, "supported": 2, "backed": 3}
    if sort_mode == "score":
        return {"backed": 0, "supported": 1, "mixed": 2, "weak": 3}
    return {"backed": 0, "supported": 1, "mixed": 2, "weak": 3}


def _candidate_view_bucket(row: Dict[str, Any]) -> str:
    support_level = str(row.get("catalyst_support_level") or "weak")
    return SUPPORT_LEVEL_LABELS.get(support_level, SUPPORT_LEVEL_LABELS["weak"])


def _serialize_candidate_row(row: Dict[str, Any], sort_mode: str = 'catalyst_first', view_rank: Optional[int] = None) -> Dict[str, Any]:
    source = deserialize_candidate(dict(row)) if any(key.endswith('_json') for key in row.keys()) else dict(row)
    ticker = source.get('ticker')
    serialized = {
        'ticker': ticker,
        'company_name': source.get('company_name'),
        'sector': source.get('sector'),
        'industry': source.get('industry'),
        'overall_score': source.get('overall_score'),
        'structural_score': source.get('structural_score'),
        'catalyst_score': source.get('catalyst_score'),
        'timing_score': source.get('timing_score'),
        'confidence_band': source.get('confidence_band'),
        'opportunity_type': source.get('opportunity_type'),
        'catalyst_truth_label': source.get('catalyst_truth_label'),
        'catalyst_support_level': source.get('catalyst_support_level'),
        'catalyst_high_credibility_count': source.get('catalyst_high_credibility_count', 0),
        'catalyst_low_signal_ratio': source.get('catalyst_low_signal_ratio', 0.0),
        'evidence_completeness_pct': source.get('evidence_completeness_pct'),
        'data_quality_label': source.get('data_quality_label'),
        'price_last_timestamp': source.get('price_last_timestamp'),
        'news_last_timestamp': source.get('news_last_timestamp'),
        'reason_codes': source.get('reason_codes') or [],
        'risk_flags': source.get('risk_flags') or [],
        'latest_news': source.get('latest_news') or [],
        'technical_summary': source.get('technical_summary'),
        'fundamental_summary': source.get('fundamental_summary'),
        'feature_snapshot': source.get('feature_snapshot') or {},
        'score_rank': int(source.get('rank') or 0),
        'view_rank': view_rank,
        'sort_mode': sort_mode,
        'view_bucket': _candidate_view_bucket(source),
    }
    return sanitize_row(serialized)


def _artifact_integrity_report(run_dir: Path, ranked_csv_rows: List[Dict[str, Any]], ranked_json_rows: List[Dict[str, Any]], shortlist_views: Dict[str, Any]) -> Dict[str, Any]:
    required_artifacts = [
        'scan_summary.json', 'coverage_diagnostics.json', 'score_diagnostics.json', 'shortlist_views.json',
        'artifact_manifest.json', 'ranked_candidates.csv', 'ranked_candidates.json', 'reasons_by_ticker.json',
        'raw_or_normalized_feature_snapshot.csv', 'config_used.json', 'scan_log.txt', 'outcome_tracking_summary.json',
    ]
    present = sorted(p.name for p in run_dir.iterdir() if p.is_file())
    issues = []
    missing = sorted(set(required_artifacts) - set(present))
    if missing:
        issues.append('Missing artifacts: ' + ', '.join(missing))
    required_rank_fields = {'score_rank', 'view_rank'}
    csv_fields = set(ranked_csv_rows[0].keys()) if ranked_csv_rows else set()
    json_fields = set(ranked_json_rows[0].keys()) if ranked_json_rows else set()
    missing_csv_fields = sorted(required_rank_fields - csv_fields)
    missing_json_fields = sorted(required_rank_fields - json_fields)
    if missing_csv_fields:
        issues.append('CSV missing rank fields: ' + ', '.join(missing_csv_fields))
    if missing_json_fields:
        issues.append('JSON missing rank fields: ' + ', '.join(missing_json_fields))
    if SHORTLIST_DEFAULT_SORT_MODE not in shortlist_views:
        issues.append(f'Default shortlist view {SHORTLIST_DEFAULT_SORT_MODE} not present in shortlist_views.json')
    return {
        'status': 'pass' if not issues else 'fail',
        'required_artifacts': required_artifacts,
        'artifacts_present': present,
        'required_rank_fields': sorted(required_rank_fields),
        'csv_fields_present': sorted(csv_fields),
        'json_fields_present': sorted(json_fields),
        'issues': issues,
        'checked_at': utc_now_iso(),
    }


def _build_outcome_rows(shortlist_rows: List[Dict[str, Any]], bundle_map: Dict[str, TickerDataBundle], settings) -> List[Dict[str, Any]]:
    rows = []
    now = utc_now_iso()
    for row in shortlist_rows:
        bundle = bundle_map.get(row['ticker'])
        frame = bundle.price_history if bundle and bundle.price_history is not None and not bundle.price_history.empty else None
        entry_price = None
        entry_date = None
        if frame is not None and not frame.empty:
            entry_price = float(frame['Close'].iloc[-1])
            entry_date = _frame_last_timestamp(frame)
        rows.append({
            'run_id': row['run_id'],
            'ticker': row['ticker'],
            'company_name': row.get('company_name'),
            'entry_date': entry_date or utc_now_iso(),
            'entry_price': entry_price,
            'target_up_pct': settings.outcome_target_up_pct,
            'stop_down_pct': settings.outcome_stop_down_pct,
            'horizon_days': settings.outcome_horizon_days,
            'status': 'pending',
            'evaluated_at': None,
            'days_elapsed': 0,
            'max_return_pct': None,
            'min_return_pct': None,
            'end_return_pct': None,
            'hit_up_first': 0,
            'hit_down_first': 0,
            'outcome_note': 'Awaiting forward evaluation',
            'updated_at': now,
        })
    return rows


def _filter_forward_frame(frame: pd.DataFrame, entry_dt: datetime) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    entry_ts = pd.Timestamp(entry_dt)
    index = frame.index
    index_tz = getattr(index, 'tz', None)
    entry_tz = getattr(entry_ts, 'tzinfo', None)

    if index_tz is None and entry_tz is not None:
        entry_ts = entry_ts.tz_localize(None)
    elif index_tz is not None and entry_tz is None:
        entry_ts = entry_ts.tz_localize(index_tz)
    elif index_tz is not None and entry_tz is not None:
        entry_ts = entry_ts.tz_convert(index_tz)

    return frame[index > entry_ts]


def _evaluate_outcomes(provider, settings) -> Dict[str, Any]:
    pending = list_shortlist_outcomes(limit=200, status='pending')
    if not pending:
        return summarize_shortlist_outcomes()
    rows_to_update = []
    now = datetime.now(timezone.utc)
    for row in pending:
        try:
            entry_dt = _parse_dt(row.get('entry_date'))
            entry_price = row.get('entry_price')
            if not entry_dt or not entry_price:
                row.update({
                    'status': 'insufficient_data',
                    'evaluated_at': utc_now_iso(),
                    'outcome_note': 'Missing entry timestamp or price',
                    'updated_at': utc_now_iso(),
                })
                rows_to_update.append(row)
                continue
            horizon_end = entry_dt + timedelta(days=int(row.get('horizon_days') or settings.outcome_horizon_days) + 5)
            lookback_days = max(settings.outcome_recheck_lookback_days, int((now - entry_dt).days) + 5)
            frame = provider.fetch_bulk_price_history([row['ticker']], lookback_days).get(row['ticker'])
            if frame is None or frame.empty:
                row.update({'evaluated_at': utc_now_iso(), 'outcome_note': 'Price history unavailable for evaluation', 'updated_at': utc_now_iso()})
                rows_to_update.append(row)
                continue
            frame = _filter_forward_frame(frame, entry_dt)
            if frame.empty:
                if now >= horizon_end:
                    row.update({'status': 'insufficient_data', 'evaluated_at': utc_now_iso(), 'outcome_note': 'No forward price rows available by horizon end', 'updated_at': utc_now_iso()})
                else:
                    row.update({'status': 'pending', 'evaluated_at': utc_now_iso(), 'outcome_note': 'Awaiting first forward price row', 'updated_at': utc_now_iso()})
                rows_to_update.append(row)
                continue
            max_return_pct = float((frame['High'].max() / entry_price) - 1.0)
            min_return_pct = float((frame['Low'].min() / entry_price) - 1.0)
            end_return_pct = float((frame['Close'].iloc[-1] / entry_price) - 1.0)
            target = float(row.get('target_up_pct') or settings.outcome_target_up_pct)
            stop = float(row.get('stop_down_pct') or settings.outcome_stop_down_pct)
            hit_up_idx = None
            hit_down_idx = None
            for idx, candle in frame.iterrows():
                if hit_up_idx is None and float(candle['High']) >= entry_price * (1 + target):
                    hit_up_idx = idx
                if hit_down_idx is None and float(candle['Low']) <= entry_price * (1 - stop):
                    hit_down_idx = idx
                if hit_up_idx is not None and hit_down_idx is not None:
                    break
            days_elapsed = max(int((min(now, horizon_end) - entry_dt).days), 0)
            status = 'pending'
            note = 'Awaiting more forward data'
            hit_up_first = 0
            hit_down_first = 0
            if hit_up_idx is not None and (hit_down_idx is None or hit_up_idx <= hit_down_idx):
                status = 'target_hit'
                note = 'Touched +5% before -3%'
                hit_up_first = 1
            elif hit_down_idx is not None and (hit_up_idx is None or hit_down_idx < hit_up_idx):
                status = 'stop_hit'
                note = 'Touched -3% before +5%'
                hit_down_first = 1
            elif now >= horizon_end:
                status = 'expired'
                note = 'Reached horizon without touching target or stop first'
            row.update({
                'status': status,
                'evaluated_at': utc_now_iso(),
                'days_elapsed': days_elapsed,
                'max_return_pct': round(max_return_pct, 4),
                'min_return_pct': round(min_return_pct, 4),
                'end_return_pct': round(end_return_pct, 4),
                'hit_up_first': hit_up_first,
                'hit_down_first': hit_down_first,
                'outcome_note': note,
                'updated_at': utc_now_iso(),
            })
            rows_to_update.append(row)
        except Exception as exc:
            row.update({
                'status': 'pending',
                'evaluated_at': utc_now_iso(),
                'outcome_note': f'Outcome evaluation deferred: {exc}',
                'updated_at': utc_now_iso(),
            })
            rows_to_update.append(row)
    upsert_shortlist_outcomes(rows_to_update)
    return summarize_shortlist_outcomes()


def _sort_rows_for_view(rows: List[Dict[str, Any]], sort_mode: str) -> List[Dict[str, Any]]:
    mode = normalize_sort_mode(sort_mode)
    if mode == "score":
        return sorted(
            rows,
            key=lambda row: (
                int(row.get("rank") or 999999),
                -float(row.get("overall_score") or 0.0),
                -float(row.get("catalyst_score") or 0.0),
            ),
        )

    priority = _support_priority(mode)
    return sorted(
        rows,
        key=lambda row: (
            priority.get(str(row.get("catalyst_support_level") or "weak"), 99),
            -float(row.get("overall_score") or 0.0),
            -float(row.get("catalyst_score") or 0.0),
            -float(row.get("timing_score") or 0.0),
            int(row.get("rank") or 999999),
        ),
    )


def _rows_with_display_metadata(rows: List[Dict[str, Any]], sort_mode: str) -> List[Dict[str, Any]]:
    ordered = _sort_rows_for_view(rows, sort_mode)
    display_rows: List[Dict[str, Any]] = []
    for display_rank, row in enumerate(ordered, start=1):
        enriched = _serialize_candidate_row(row, sort_mode=sort_mode, view_rank=display_rank)
        enriched["display_rank"] = display_rank
        display_rows.append(enriched)
    return display_rows


def _candidate_groups(rows: List[Dict[str, Any]], sort_mode: str) -> List[Dict[str, Any]]:
    mode = normalize_sort_mode(sort_mode)
    if mode == "score":
        return [{
            "key": "all",
            "label": SORT_MODE_LABELS[mode],
            "description": "Flat score-ranked view. Score rank is the primary order.",
            "rows": rows,
            "count": len(rows),
        }]

    if mode == "quality_first":
        group_order = [
            ("mixed", "Quality/momentum · mixed catalyst", "Quality/momentum names with some catalyst evidence, but not enough to call them catalyst-backed."),
            ("weak", "Quality/momentum · weak catalyst", "Quality and timing names where catalyst evidence is weak or unconfirmed."),
            ("supported", "Catalyst-supported", "Multiple supportive headlines, but not strong enough to call fully catalyst-backed."),
            ("backed", "Catalyst-backed", "Highest-truth catalyst names with stronger headline support."),
        ]
    else:
        group_order = [
            ("backed", "Catalyst-backed", "Highest-truth catalyst names with stronger headline support."),
            ("supported", "Catalyst-supported", "Multiple supportive headlines, but not strong enough to call fully catalyst-backed."),
            ("mixed", "Quality/momentum · mixed catalyst", "Quality/momentum names with some catalyst evidence, but not enough to call them catalyst-backed."),
            ("weak", "Quality/momentum · weak catalyst", "Quality and timing names where catalyst evidence is weak or unconfirmed."),
        ]

    grouped: List[Dict[str, Any]] = []
    for level, label, description in group_order:
        group_rows = [row for row in rows if str(row.get("catalyst_support_level") or "weak") == level]
        grouped.append({
            "key": level,
            "label": label,
            "description": description,
            "rows": group_rows,
            "count": len(group_rows),
        })
    return grouped


def _view_mix(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "total": len(rows),
        "catalyst_backed": sum(1 for row in rows if str(row.get("catalyst_support_level") or "") == "backed"),
        "catalyst_supported": sum(1 for row in rows if str(row.get("catalyst_support_level") or "") == "supported"),
        "quality_momentum_mixed": sum(1 for row in rows if str(row.get("catalyst_support_level") or "") == "mixed"),
        "quality_momentum_weak": sum(1 for row in rows if str(row.get("catalyst_support_level") or "") == "weak"),
    }


def _shortlist_views_payload(rows: List[Dict[str, Any]], shortlist_size: int) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for mode in SORT_MODE_LABELS:
        ordered = _rows_with_display_metadata(rows, mode)
        shortlisted = ordered[:shortlist_size]
        payload[mode] = {
            "sort_mode": mode,
            "label": SORT_MODE_LABELS[mode],
            "distribution": _view_mix(shortlisted),
            "rows": [
                {
                    "display_rank": row.get("display_rank"),
                    "score_rank": row.get("score_rank"),
                    "ticker": row.get("ticker"),
                    "company_name": row.get("company_name"),
                    "overall_score": row.get("overall_score"),
                    "catalyst_score": row.get("catalyst_score"),
                    "timing_score": row.get("timing_score"),
                    "opportunity_type": row.get("opportunity_type"),
                    "catalyst_truth_label": row.get("catalyst_truth_label"),
                    "catalyst_support_level": row.get("catalyst_support_level"),
                    "view_bucket": row.get("view_bucket"),
                    "confidence_band": row.get("confidence_band"),
                }
                for row in shortlisted
            ],
        }
    return payload


def _score_diagnostics(rows: List[Dict[str, Any]], shortlist_size: int) -> Dict[str, Any]:
    def stats(values: List[float]) -> Dict[str, Any]:
        if not values:
            return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None, "mean": None, "std": None, "pct_ge_90": 0.0, "pct_eq_100": 0.0}
        series = pd.Series(values, dtype=float)
        return {
            "count": int(series.count()),
            "min": round(float(series.min()), 2),
            "p25": round(float(series.quantile(0.25)), 2),
            "median": round(float(series.median()), 2),
            "p75": round(float(series.quantile(0.75)), 2),
            "max": round(float(series.max()), 2),
            "mean": round(float(series.mean()), 2),
            "std": round(float(series.std(ddof=0)), 2),
            "pct_ge_90": round(float((series >= 90).mean()) * 100, 1),
            "pct_eq_100": round(float((series == 100).mean()) * 100, 1),
        }

    metrics = ["overall_score", "structural_score", "catalyst_score", "timing_score"]
    shortlist_rows = rows[:shortlist_size]
    payload = {
        "ranked": {metric: stats([float(row.get(metric, 0) or 0) for row in rows]) for metric in metrics},
        "shortlist": {metric: stats([float(row.get(metric, 0) or 0) for row in shortlist_rows]) for metric in metrics},
    }
    payload["flags"] = {
        "timing_saturation_warning": payload["ranked"]["timing_score"]["pct_eq_100"] >= 50.0,
        "timing_score_pct_eq_100": payload["ranked"]["timing_score"]["pct_eq_100"],
        "catalyst_score_pct_ge_90": payload["ranked"]["catalyst_score"]["pct_ge_90"],
    }
    payload["shortlist_view_distributions"] = {
        mode: _view_mix(_sort_rows_for_view(rows, mode)[:shortlist_size])
        for mode in SORT_MODE_LABELS
    }
    return payload

def run_scan_now(request_key: Optional[str] = None) -> str:
    global LAST_SCAN_STARTED_AT, LAST_IDEMPOTENCY_KEY
    settings = load_settings()
    now = time.time()
    with STATUS_LOCK:
        active_run_id = SCAN_STATUS.get("run_id")
        if SCAN_STATUS.get("is_running"):
            if request_key and request_key == LAST_IDEMPOTENCY_KEY and active_run_id:
                return str(active_run_id)
            raise ScanAlreadyRunningError("A scan is already in progress.")
        if now - LAST_SCAN_STARTED_AT < settings.scan_cooldown_seconds:
            if request_key and request_key == LAST_IDEMPOTENCY_KEY and active_run_id:
                return str(active_run_id)
            raise ScanCooldownError("A scan was started very recently. Please wait a few seconds and try again.")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        LAST_SCAN_STARTED_AT = now
        LAST_IDEMPOTENCY_KEY = request_key
        SCAN_STATUS["is_running"] = True
        SCAN_STATUS["run_id"] = run_id
        SCAN_STATUS["request_key"] = request_key
        SCAN_STATUS["phase"] = "starting"
        SCAN_STATUS["message"] = "Preparing scan"
        SCAN_STATUS["updated_at"] = utc_now_iso()
    thread = threading.Thread(target=_run_scan_thread, args=(run_id,), daemon=True)
    thread.start()
    return run_id


def _run_scan_thread(run_id: str) -> None:
    settings = load_settings()
    provider = get_provider(settings.default_provider, settings.demo_mode, settings.max_workers)
    started_at = utc_now_iso()
    run_dir = ensure_dir(Path(settings.artifacts_dir) / run_id)
    warnings: List[str] = []
    log_lines: List[str] = [f"[{started_at}] Starting scan {run_id}"]
    _update_status(is_running=True, run_id=run_id, phase="starting", message="Preparing scan", progress_current=0, progress_total=1)
    upsert_run(_build_run_record(run_id, settings, "running", started_at, message="Preparing scan", phase="starting", artifacts_dir=str(run_dir)))

    try:
        _update_status(phase="loading_universe", message="Loading universe", progress_current=0, progress_total=1)
        all_universe_rows = load_universe()
        all_universe_rows = [{**row, "symbol": _normalize_ticker(row["symbol"]), "sector": normalize_sector_name(row.get("sector", ""))} for row in all_universe_rows if row.get("symbol")]
        if not all_universe_rows:
            raise RuntimeError("Universe loading failed; no symbols available.")
        universe_rows = all_universe_rows[: settings.scan_ticker_limit]
        if settings.scan_ticker_limit < len(all_universe_rows):
            warnings.append(f"Configured scan_ticker_limit {settings.scan_ticker_limit} is narrower than available universe size {len(all_universe_rows)}.")
        log_lines.append(f"Universe rows available: {len(all_universe_rows)}")
        log_lines.append(f"Universe rows loaded: {len(universe_rows)}")

        benchmark_tickers = ["SPY"]
        seen_sector_etfs = []
        for row in universe_rows:
            etf = SECTOR_ETF_MAP.get(normalize_sector_name(row.get("sector", "")))
            if etf and etf not in seen_sector_etfs:
                seen_sector_etfs.append(etf)
        bulk_tickers = [row["symbol"] for row in universe_rows] + benchmark_tickers + seen_sector_etfs

        _update_status(phase="fetching_prices", message="Fetching bulk price history", progress_current=0, progress_total=len(bulk_tickers))
        history_map = provider.fetch_bulk_price_history(bulk_tickers, settings.lookback_days)
        log_lines.append(f"Bulk price histories fetched: {len(history_map)}")

        enriched_universe_rows, prefilter_diagnostics = _initial_prefilter(history_map, universe_rows, settings)
        log_lines.append(f"Prefilter kept {len(enriched_universe_rows)} tickers for full enrichment")
        if not enriched_universe_rows:
            enriched_universe_rows = universe_rows[: min(len(universe_rows), settings.enrichment_limit)]
            warnings.append("Prefilter returned no rows; fell back to the first slice of universe rows.")

        candidate_rows: List[Dict[str, Any]] = []
        feature_rows: List[Dict[str, Any]] = []
        total = len(enriched_universe_rows)
        _update_status(phase="enriching_tickers", message="Fetching fundamentals and news", progress_current=0, progress_total=max(total, 1))

        benchmark_spy = history_map.get("SPY")
        bundles: Dict[str, TickerDataBundle] = {}
        if hasattr(provider, "fetch_many_ticker_bundles"):
            bundles = provider.fetch_many_ticker_bundles(
                [row["symbol"] for row in enriched_universe_rows],
                settings.lookback_days,
                settings.news_lookback_days,
            )
        else:
            for index, row in enumerate(enriched_universe_rows, start=1):
                bundles[row["symbol"]] = provider.fetch_ticker_bundle(row["symbol"], settings.lookback_days, settings.news_lookback_days)
                _update_status(phase="enriching_tickers", message=f"Enriched {row['symbol']}", progress_current=index, progress_total=total)

        for index, row in enumerate(enriched_universe_rows, start=1):
            ticker = row["symbol"]
            bundle = bundles.get(ticker) or TickerDataBundle(ticker=ticker, warnings=["No bundle returned"])
            bundle.company_name = bundle.company_name or row.get("name", ticker)
            bundle.sector = normalize_sector_name(bundle.sector or row.get("sector", ""))
            bundle.industry = bundle.industry or row.get("industry", "")
            price_history = bundle.price_history if bundle.price_history is not None and not bundle.price_history.empty else history_map.get(ticker)
            sector_etf = SECTOR_ETF_MAP.get(bundle.sector or row.get("sector", ""))
            sector_history = history_map.get(sector_etf) if sector_etf else None

            timing_metrics = compute_timing_metrics(price_history, benchmark_spy, sector_history)
            timing_score, timing_reasons, timing_risks, technical_summary = score_timing(timing_metrics)
            structural_score, structural_reasons, structural_risks, fundamental_summary = score_structural(bundle)
            catalyst_score, catalyst_reasons, catalyst_risks, catalyst_metrics, prepared_news = score_catalyst(bundle.news, ticker=ticker, company_name=bundle.company_name)

            evidence_quality = _evidence_quality(bundle, timing_metrics, prepared_news, settings)
            combined_reasons = list(dict.fromkeys(structural_reasons + catalyst_reasons + timing_reasons))
            combined_risks = [item for item in dict.fromkeys(bundle.warnings + structural_risks + catalyst_risks + timing_risks + evidence_quality["staleness_flags"]) if item]
            raw_overall_score = structural_score * settings.structural_weight + catalyst_score * settings.catalyst_weight + timing_score * settings.timing_weight
            overall_score = round(max(raw_overall_score - float(catalyst_metrics.get("rank_penalty", 0.0) or 0.0), 0.0), 2)
            opportunity_type = catalyst_metrics.get("opportunity_type", "Quality/momentum opportunity")
            catalyst_truth_label = catalyst_metrics.get("truth_label", "Catalyst weak / unconfirmed")
            catalyst_support_level = catalyst_metrics.get("support_level", "weak")
            confidence = confidence_band(overall_score, catalyst_support_level)
            candidate_rows.append({
                "run_id": run_id,
                "rank": 0,
                "ticker": ticker,
                "company_name": bundle.company_name,
                "sector": bundle.sector,
                "industry": bundle.industry,
                "cik": row.get("cik", ""),
                "overall_score": overall_score,
                "structural_score": structural_score,
                "catalyst_score": catalyst_score,
                "timing_score": timing_score,
                "confidence_band": confidence,
                "opportunity_type": opportunity_type,
                "catalyst_truth_label": catalyst_truth_label,
                "catalyst_support_level": catalyst_support_level,
                "catalyst_high_credibility_count": int(catalyst_metrics.get("high_credibility_relevant_count") or 0),
                "catalyst_low_signal_ratio": float(catalyst_metrics.get("low_signal_relevant_ratio") or 0.0),
                "evidence_completeness_pct": float(evidence_quality.get("evidence_completeness_pct") or 0.0),
                "data_quality_label": evidence_quality.get("data_quality_label"),
                "price_last_timestamp": evidence_quality.get("price_last_timestamp"),
                "news_last_timestamp": evidence_quality.get("news_last_timestamp"),
                "reason_codes_json": json.dumps(combined_reasons[:6]),
                "risk_flags_json": json.dumps(combined_risks[:6]),
                "latest_news_json": json.dumps(prepared_news[:5]),
                "technical_summary": technical_summary,
                "fundamental_summary": fundamental_summary,
                "feature_snapshot_json": json.dumps({"timing": timing_metrics, "fundamentals": bundle.fundamentals, "catalyst": catalyst_metrics}, default=str),
            })
            feature_rows.append(sanitize_row({
                "ticker": ticker,
                "company_name": bundle.company_name,
                "sector": bundle.sector,
                "overall_score": overall_score,
                "structural_score": structural_score,
                "catalyst_score": catalyst_score,
                "timing_score": timing_score,
                **{f"timing_{k}": v for k, v in timing_metrics.items() if k != "warnings"},
                **{f"fund_{k}": v for k, v in (bundle.fundamentals or {}).items()},
                **{f"catalyst_{k}": v for k, v in catalyst_metrics.items()},
                "opportunity_type": opportunity_type,
                "catalyst_truth_label": catalyst_truth_label,
                "catalyst_support_level": catalyst_support_level,
                "risk_flags": " | ".join(combined_risks[:6]) if combined_risks else "None identified",
                "reason_codes": " | ".join(combined_reasons[:6]) if combined_reasons else "No explicit reason codes",
                "latest_news_titles": " | ".join(item.get("title", "") for item in prepared_news[:3]) or "No relevant headlines retained",
                "evidence_completeness_pct": evidence_quality.get("evidence_completeness_pct"),
                "data_quality_label": evidence_quality.get("data_quality_label"),
                "price_last_timestamp": evidence_quality.get("price_last_timestamp"),
                "news_last_timestamp": evidence_quality.get("news_last_timestamp"),
                "price_age_days": evidence_quality.get("price_age_days"),
                "news_age_days": evidence_quality.get("news_age_days"),
            }))
            _update_status(phase="scoring", message=f"Scored {ticker}", progress_current=index, progress_total=total)

        deduped_rows, dedupe_messages = _dedupe_share_classes(candidate_rows)
        if dedupe_messages:
            warnings.extend(dedupe_messages[:10])
            log_lines.extend(f"[{utc_now_iso()}] {msg}" for msg in dedupe_messages[:20])
        deduped_tickers = {row["ticker"] for row in deduped_rows}
        feature_rows = [row for row in feature_rows if row["ticker"] in deduped_tickers]

        ranked_rows = sorted(deduped_rows, key=lambda x: x["overall_score"], reverse=True)
        for idx, row in enumerate(ranked_rows, start=1):
            row["rank"] = idx
        replace_candidates(run_id, ranked_rows)
        shortlist_rows = ranked_rows[: settings.shortlist_size]
        shortlist_views = _shortlist_views_payload(ranked_rows, settings.shortlist_size)
        view_rank_maps = {
            mode: {item["ticker"]: item["display_rank"] for item in _rows_with_display_metadata(ranked_rows, mode)}
            for mode in SORT_MODE_LABELS
        }

        feature_coverage = _coverage_counts(feature_rows, [
            "fund_revenueGrowth",
            "fund_earningsGrowth",
            "fund_profitMargins",
            "fund_operatingMargins",
            "fund_debtToEquity",
            "fund_currentRatio",
            "fund_returnOnEquity",
            "fund_forwardPE",
            "fund_freeCashflowYield",
            "timing_relative_strength_vs_spy",
            "timing_relative_strength_vs_sector",
            "catalyst_ticker_relevant_headline_count",
            "catalyst_high_credibility_relevant_count",
            "catalyst_low_signal_relevant_count",
            "catalyst_unique_relevant_publishers",
            "catalyst_low_signal_relevant_ratio",
        ])
        degradation_summary = _run_degradation_assessment(
            feature_rows,
            feature_coverage,
            settings,
            prefilter_diagnostics["price_history_available_count"],
            len(universe_rows),
        )
        coverage_diagnostics = {
            "funnel": {
                "universe_available": len(all_universe_rows),
                "universe_loaded": len(universe_rows),
                "price_history_available": prefilter_diagnostics["price_history_available_count"],
                "price_history_missing": prefilter_diagnostics["price_history_missing_count"],
                "enrichment_selected": len(enriched_universe_rows),
                "bundle_count": len(bundles),
                "ranked_after_dedupe": len(ranked_rows),
                "shortlist_size": len(shortlist_rows),
            },
            "feature_coverage": feature_coverage,
            "data_quality_distribution": {
                "high": sum(1 for row in feature_rows if row.get("data_quality_label") == "High"),
                "moderate": sum(1 for row in feature_rows if row.get("data_quality_label") == "Moderate"),
                "thin": sum(1 for row in feature_rows if row.get("data_quality_label") == "Thin"),
            },
            "degradation_summary": degradation_summary,
            "catalyst_truth_distribution": {
                "backed": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "backed"),
                "supported": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "supported"),
                "mixed": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "mixed"),
                "weak": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "weak"),
            },
            "shortlist_view_distributions": {mode: payload["distribution"] for mode, payload in shortlist_views.items()},
            "examples": {
                "missing_price_history_examples": prefilter_diagnostics.get("missing_price_history_examples", []),
            },
        }

        score_diagnostics = _score_diagnostics(ranked_rows, settings.shortlist_size)
        score_diagnostics["catalyst_truth_distribution"] = coverage_diagnostics["catalyst_truth_distribution"]
        score_diagnostics["opportunity_type_distribution"] = {
            "catalyst_backed": sum(1 for row in ranked_rows if row.get("opportunity_type") == "Catalyst-backed opportunity"),
            "catalyst_supported": sum(1 for row in ranked_rows if row.get("opportunity_type") == "Catalyst-supported opportunity"),
            "quality_momentum": sum(1 for row in ranked_rows if row.get("opportunity_type") == "Quality/momentum opportunity"),
        }
        score_diagnostics["data_quality_distribution"] = coverage_diagnostics["data_quality_distribution"]
        score_diagnostics["degradation_summary"] = degradation_summary

        outcome_rows = _build_outcome_rows(shortlist_rows, bundles, settings)
        upsert_shortlist_outcomes(outcome_rows)
        outcome_summary = _evaluate_outcomes(provider, settings)

        scan_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "ended_at": utc_now_iso(),
            "provider": provider.provider_name,
            "universe_name": settings.default_universe_name,
            "universe_available": len(all_universe_rows),
            "universe_size_loaded": len(universe_rows),
            "bulk_price_ticker_count": len(bulk_tickers),
            "price_history_available_count": prefilter_diagnostics["price_history_available_count"],
            "price_history_missing_count": prefilter_diagnostics["price_history_missing_count"],
            "enrichment_size": len(enriched_universe_rows),
            "bundle_count": len(bundles),
            "deduped_candidate_count": len(ranked_rows),
            "shortlist_size": len(shortlist_rows),
            "default_latest_results_view": SHORTLIST_DEFAULT_SORT_MODE,
            "score_weights": {"structural": settings.structural_weight, "catalyst": settings.catalyst_weight, "timing": settings.timing_weight},
            "configured_limits": {"scan_ticker_limit": settings.scan_ticker_limit, "enrichment_limit": settings.enrichment_limit, "shortlist_size": settings.shortlist_size},
            "build": {
                "app_version": settings.app_version,
                "build_id": settings.build_id,
                "build_timestamp_utc": settings.build_timestamp_utc,
                "artifact_schema_version": settings.artifact_schema_version,
            },
            "degradation_summary": degradation_summary,
            "outcome_tracking_summary": outcome_summary,
            "note": "Version 1 outputs opportunity scores, not calibrated probabilities.",
            "warnings": warnings,
            "score_diagnostics": score_diagnostics,
            "shortlist_view_distributions": {mode: payload["distribution"] for mode, payload in shortlist_views.items()},
        }

        reasons_by_ticker = {
            row["ticker"]: {
                "reason_codes": json.loads(row["reason_codes_json"]),
                "risk_flags": json.loads(row["risk_flags_json"]),
                "latest_news": json.loads(row["latest_news_json"]),
                "catalyst_truth_label": row.get("catalyst_truth_label"),
                "catalyst_support_level": row.get("catalyst_support_level"),
                "opportunity_type": row.get("opportunity_type"),
            }
            for row in ranked_rows
        }
        ranked_json_rows = []
        ranked_csv_rows = []
        for row in ranked_rows:
            converted = _serialize_candidate_row(
                row,
                sort_mode=SHORTLIST_DEFAULT_SORT_MODE,
                view_rank=view_rank_maps.get(SHORTLIST_DEFAULT_SORT_MODE, {}).get(row.get("ticker")),
            )
            converted["catalyst_first_rank"] = view_rank_maps.get("catalyst_first", {}).get(converted.get("ticker"))
            converted["quality_first_rank"] = view_rank_maps.get("quality_first", {}).get(converted.get("ticker"))
            converted["display_rank"] = converted.get("view_rank")
            ranked_json_rows.append(converted)
            ranked_csv_rows.append(sanitize_row({
                k: v for k, v in converted.items() if k not in {"latest_news", "feature_snapshot"}
            } | {
                "reason_codes": " | ".join(converted.get("reason_codes") or []) or "No explicit reason codes",
                "risk_flags": " | ".join(converted.get("risk_flags") or []) or "None identified",
                "latest_news_titles": " | ".join(item.get("title", "") for item in (converted.get("latest_news") or [])[:3]) or "No relevant headlines retained",
                "latest_news_publishers": " | ".join(item.get("publisher", "") for item in (converted.get("latest_news") or [])[:3]) or "No relevant publishers retained",
            }))

        artifact_manifest = {
            "run_id": run_id,
            "default_latest_results_view": SHORTLIST_DEFAULT_SORT_MODE,
            "build": {
                "app_version": settings.app_version,
                "build_id": settings.build_id,
                "build_timestamp_utc": settings.build_timestamp_utc,
                "artifact_schema_version": settings.artifact_schema_version,
            },
            "required_artifacts": [
                "scan_summary.json",
                "coverage_diagnostics.json",
                "score_diagnostics.json",
                "shortlist_views.json",
                "artifact_manifest.json",
                "ranked_candidates.csv",
                "ranked_candidates.json",
                "reasons_by_ticker.json",
                "raw_or_normalized_feature_snapshot.csv",
                "config_used.json",
                "scan_log.txt",
                "outcome_tracking_summary.json",
            ],
            "artifacts_present_before_zip": sorted([p.name for p in run_dir.iterdir() if p.is_file()]),
        }

        write_json(run_dir / "scan_summary.json", scan_summary)
        write_json(run_dir / "coverage_diagnostics.json", coverage_diagnostics)
        write_json(run_dir / "score_diagnostics.json", score_diagnostics)
        write_json(run_dir / "shortlist_views.json", shortlist_views)
        write_csv(run_dir / "ranked_candidates.csv", ranked_csv_rows)
        write_json(run_dir / "ranked_candidates.json", ranked_json_rows)
        write_json(run_dir / "reasons_by_ticker.json", reasons_by_ticker)
        write_csv(run_dir / "raw_or_normalized_feature_snapshot.csv", feature_rows)
        write_json(run_dir / "config_used.json", sanitize_settings_payload(settings.to_dict()))
        write_json(run_dir / "outcome_tracking_summary.json", {
            "summary": outcome_summary,
            "recent": list_shortlist_outcomes(limit=100),
            "target_definition": {
                "target_up_pct": settings.outcome_target_up_pct,
                "stop_down_pct": settings.outcome_stop_down_pct,
                "horizon_days": settings.outcome_horizon_days,
            },
        })
        write_text(run_dir / "scan_log.txt", "\n".join(log_lines + [f"[{utc_now_iso()}] Completed successfully"]))
        write_json(run_dir / "artifact_manifest.json", artifact_manifest)
        integrity_report = _artifact_integrity_report(run_dir, ranked_csv_rows, ranked_json_rows, shortlist_views)
        artifact_manifest["artifacts_present_before_zip"] = sorted([p.name for p in run_dir.iterdir() if p.is_file()])
        artifact_manifest["artifact_integrity"] = integrity_report
        write_json(run_dir / "artifact_manifest.json", artifact_manifest)
        if integrity_report["status"] != "pass":
            raise RuntimeError("Artifact integrity failed: " + "; ".join(integrity_report["issues"]))
        zip_path = run_dir / f"{run_id}_scan_pack.zip"
        zip_directory(run_dir, zip_path)

        ended_at = utc_now_iso()
        scan_summary["artifact_integrity"] = integrity_report
        write_json(run_dir / "scan_summary.json", scan_summary)
        upsert_run(_build_run_record(run_id, settings, "completed", started_at, message="Scan completed", ended_at=ended_at, progress_current=total, progress_total=total, phase="completed", universe_size=len(universe_rows), enrichment_size=len(enriched_universe_rows), shortlist_size=len(shortlist_rows), warnings=warnings, artifacts_dir=str(run_dir), artifact_zip_path=str(zip_path), summary=scan_summary))
        _update_status(is_running=False, run_id=run_id, phase="completed", message="Scan completed", progress_current=total, progress_total=total)
    except Exception as exc:
        log_lines.append(f"[{utc_now_iso()}] FAILED: {exc}")
        write_text(run_dir / "scan_log.txt", "\n".join(log_lines))
        ended_at = utc_now_iso()
        upsert_run(_build_run_record(run_id, settings, "failed", started_at, message=str(exc), ended_at=ended_at, phase="failed", warnings=warnings + [str(exc)], artifacts_dir=str(run_dir)))
        _update_status(is_running=False, run_id=run_id, phase="failed", message=str(exc), progress_current=0, progress_total=0)


def latest_run_with_candidates(sort_mode: str = "catalyst_first") -> Optional[Dict[str, Any]]:
    runs = list_runs(limit=1)
    if not runs:
        return None
    run = deserialize_run(runs[0])
    if run:
        run = _sanitize_run_for_status(run)
        raw_candidates = [deserialize_candidate(row) for row in list_candidates(run["run_id"])]
        mode = normalize_sort_mode(sort_mode)
        display_rows = _rows_with_display_metadata(raw_candidates, mode)
        run["candidates"] = display_rows
        run["sort_mode"] = mode
        run["sort_label"] = SORT_MODE_LABELS[mode]
        run["candidate_groups"] = _candidate_groups(display_rows, mode)
        run["view_mix"] = _view_mix(display_rows)
        run["outcome_tracking_summary"] = summarize_shortlist_outcomes()
    return run
