from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd

from .config import load_settings
from .db import deserialize_candidate, deserialize_run, list_candidates, list_runs, replace_candidates, upsert_run
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
    with STATUS_LOCK:
        status = dict(SCAN_STATUS)
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
    payload = {
        "ranked": {metric: stats([float(row.get(metric, 0) or 0) for row in rows]) for metric in metrics},
        "shortlist": {metric: stats([float(row.get(metric, 0) or 0) for row in rows[:shortlist_size]]) for metric in metrics},
    }
    payload["flags"] = {
        "timing_saturation_warning": payload["ranked"]["timing_score"]["pct_eq_100"] >= 50.0,
        "timing_score_pct_eq_100": payload["ranked"]["timing_score"]["pct_eq_100"],
        "catalyst_score_pct_ge_90": payload["ranked"]["catalyst_score"]["pct_ge_90"],
    }
    return payload

def run_scan_now() -> str:
    global LAST_SCAN_STARTED_AT
    settings = load_settings()
    now = time.time()
    with STATUS_LOCK:
        if now - LAST_SCAN_STARTED_AT < settings.scan_cooldown_seconds:
            raise ScanCooldownError("A scan was started very recently. Please wait a few seconds and try again.")
        if SCAN_STATUS.get("is_running"):
            raise ScanAlreadyRunningError("A scan is already in progress.")
        LAST_SCAN_STARTED_AT = now
        SCAN_STATUS["is_running"] = True
        SCAN_STATUS["phase"] = "starting"
        SCAN_STATUS["message"] = "Preparing scan"
        SCAN_STATUS["updated_at"] = utc_now_iso()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
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

            combined_reasons = list(dict.fromkeys(structural_reasons + catalyst_reasons + timing_reasons))
            combined_risks = [item for item in dict.fromkeys(bundle.warnings + structural_risks + catalyst_risks + timing_risks) if item]
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
            "feature_coverage": _coverage_counts(feature_rows, [
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
            ]),
            "catalyst_truth_distribution": {
                "backed": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "backed"),
                "supported": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "supported"),
                "mixed": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "mixed"),
                "weak": sum(1 for row in ranked_rows if row.get("catalyst_support_level") == "weak"),
            },
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
            "score_weights": {"structural": settings.structural_weight, "catalyst": settings.catalyst_weight, "timing": settings.timing_weight},
            "configured_limits": {"scan_ticker_limit": settings.scan_ticker_limit, "enrichment_limit": settings.enrichment_limit, "shortlist_size": settings.shortlist_size},
            "note": "Version 1 outputs opportunity scores, not calibrated probabilities.",
            "warnings": warnings,
            "score_diagnostics": score_diagnostics,
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
            converted = dict(row)
            reason_codes = json.loads(converted.pop("reason_codes_json"))
            risk_flags = json.loads(converted.pop("risk_flags_json"))
            latest_news = json.loads(converted.pop("latest_news_json"))
            feature_snapshot = json.loads(converted.pop("feature_snapshot_json"))
            converted["reason_codes"] = reason_codes
            converted["risk_flags"] = risk_flags
            converted["latest_news"] = latest_news
            converted["feature_snapshot"] = feature_snapshot
            ranked_json_rows.append(converted)
            ranked_csv_rows.append(sanitize_row({
                k: v for k, v in converted.items() if k not in {"latest_news", "feature_snapshot", "cik"}
            } | {
                "reason_codes": " | ".join(reason_codes) if reason_codes else "No explicit reason codes",
                "risk_flags": " | ".join(risk_flags) if risk_flags else "None identified",
                "latest_news_titles": " | ".join(item.get("title", "") for item in latest_news[:3]) or "No relevant headlines retained",
                "latest_news_publishers": " | ".join(item.get("publisher", "") for item in latest_news[:3]) or "No relevant publishers retained",
                "catalyst_high_credibility_count": converted.get("catalyst_high_credibility_count", 0),
                "catalyst_low_signal_ratio": converted.get("catalyst_low_signal_ratio", 0.0),
            }))

        write_json(run_dir / "scan_summary.json", scan_summary)
        write_json(run_dir / "coverage_diagnostics.json", coverage_diagnostics)
        write_json(run_dir / "score_diagnostics.json", score_diagnostics)
        write_csv(run_dir / "ranked_candidates.csv", ranked_csv_rows)
        write_json(run_dir / "ranked_candidates.json", ranked_json_rows)
        write_json(run_dir / "reasons_by_ticker.json", reasons_by_ticker)
        write_csv(run_dir / "raw_or_normalized_feature_snapshot.csv", feature_rows)
        write_json(run_dir / "config_used.json", sanitize_settings_payload(settings.to_dict()))
        write_text(run_dir / "scan_log.txt", "\n".join(log_lines + [f"[{utc_now_iso()}] Completed successfully"]))
        zip_path = run_dir / f"{run_id}_scan_pack.zip"
        zip_directory(run_dir, zip_path)

        ended_at = utc_now_iso()
        upsert_run(_build_run_record(run_id, settings, "completed", started_at, message="Scan completed", ended_at=ended_at, progress_current=total, progress_total=total, phase="completed", universe_size=len(universe_rows), enrichment_size=len(enriched_universe_rows), shortlist_size=len(shortlist_rows), warnings=warnings, artifacts_dir=str(run_dir), artifact_zip_path=str(zip_path), summary=scan_summary))
        _update_status(is_running=False, run_id=run_id, phase="completed", message="Scan completed", progress_current=total, progress_total=total)
    except Exception as exc:
        log_lines.append(f"[{utc_now_iso()}] FAILED: {exc}")
        write_text(run_dir / "scan_log.txt", "\n".join(log_lines))
        ended_at = utc_now_iso()
        upsert_run(_build_run_record(run_id, settings, "failed", started_at, message=str(exc), ended_at=ended_at, phase="failed", warnings=warnings + [str(exc)], artifacts_dir=str(run_dir)))
        _update_status(is_running=False, run_id=run_id, phase="failed", message=str(exc), progress_current=0, progress_total=0)


def latest_run_with_candidates() -> Optional[Dict[str, Any]]:
    runs = list_runs(limit=1)
    if not runs:
        return None
    run = deserialize_run(runs[0])
    if run:
        run = _sanitize_run_for_status(run)
        run["candidates"] = [deserialize_candidate(row) for row in list_candidates(run["run_id"])]
    return run
