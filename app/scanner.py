from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd

from .config import load_settings
from .db import deserialize_candidate, deserialize_run, list_candidates, list_runs, replace_candidates, upsert_run
from .providers import get_provider
from .providers.base import TickerDataBundle
from .scoring import SECTOR_ETF_MAP, compute_timing_metrics, confidence_band, score_catalyst, score_structural, score_timing
from .storage import ensure_dir, utc_now_iso, write_csv, write_json, write_text, zip_directory
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


def _persist_runtime_status(status: Dict[str, Any]) -> None:
    settings = load_settings()
    Path(settings.runtime_status_path).write_text(json.dumps(status, indent=2))



def get_runtime_status() -> Dict[str, Any]:
    with STATUS_LOCK:
        status = dict(SCAN_STATUS)
    run_history = list_runs(limit=1)
    if run_history:
        latest = deserialize_run(run_history[0])
        status["latest_run"] = latest
    return status



def _update_status(**kwargs: Any) -> None:
    with STATUS_LOCK:
        SCAN_STATUS.update(kwargs)
        SCAN_STATUS["updated_at"] = utc_now_iso()
        status = dict(SCAN_STATUS)
    _persist_runtime_status(status)



def _safe_name(value: str) -> str:
    return value.replace(":", "").replace("+", "").replace("-", "")



def _initial_prefilter(history_map: Dict[str, pd.DataFrame], universe_rows: List[Dict[str, str]], settings) -> List[Dict[str, str]]:
    benchmark_spy = history_map.get("SPY")
    scored = []
    universe_by_ticker = {row["symbol"]: row for row in universe_rows}
    for ticker, frame in history_map.items():
        if ticker in {"SPY"} or ticker.startswith("XL"):
            continue
        sector = universe_by_ticker.get(ticker, {}).get("sector", "")
        sector_etf = SECTOR_ETF_MAP.get(sector)
        sector_history = history_map.get(sector_etf) if sector_etf else None
        metrics = compute_timing_metrics(frame, benchmark_spy, sector_history)
        score, _, _, _ = score_timing(metrics)
        scored.append({"ticker": ticker, "score": score})
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    keep = {row["ticker"] for row in ranked[: settings.enrichment_limit]}
    return [row for row in universe_rows if row["symbol"] in keep]



def _normalize_ticker(symbol: str) -> str:
    return symbol.replace(".", "-").upper()



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
        "settings_json": json.dumps(settings.to_dict(), default=str),
        "warnings_json": json.dumps(extra.get("warnings", []), default=str),
        "artifacts_dir": extra.get("artifacts_dir", ""),
        "artifact_zip_path": extra.get("artifact_zip_path", ""),
        "summary_json": json.dumps(extra.get("summary", {}), default=str),
    }
    return record



def run_scan_now() -> str:
    global LAST_SCAN_STARTED_AT
    settings = load_settings()
    now = time.time()
    if now - LAST_SCAN_STARTED_AT < settings.scan_cooldown_seconds:
        raise ScanCooldownError("A scan was started very recently. Please wait a few seconds and try again.")
    with STATUS_LOCK:
        if SCAN_STATUS.get("is_running"):
            raise ScanAlreadyRunningError("A scan is already in progress.")
        LAST_SCAN_STARTED_AT = now
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
        universe_rows = load_universe()
        universe_rows = [{**row, "symbol": _normalize_ticker(row["symbol"])} for row in universe_rows]
        universe_rows = universe_rows[: settings.scan_ticker_limit]
        log_lines.append(f"Universe rows loaded: {len(universe_rows)}")

        benchmark_tickers = ["SPY"]
        seen_sector_etfs = []
        for row in universe_rows:
            etf = SECTOR_ETF_MAP.get(row.get("sector", ""))
            if etf and etf not in seen_sector_etfs:
                seen_sector_etfs.append(etf)
        bulk_tickers = [row["symbol"] for row in universe_rows] + benchmark_tickers + seen_sector_etfs

        _update_status(phase="fetching_prices", message="Fetching bulk price history", progress_current=0, progress_total=len(bulk_tickers))
        history_map = provider.fetch_bulk_price_history(bulk_tickers, settings.lookback_days)
        log_lines.append(f"Bulk price histories fetched: {len(history_map)}")

        enriched_universe_rows = _initial_prefilter(history_map, universe_rows, settings)
        log_lines.append(f"Prefilter kept {len(enriched_universe_rows)} tickers for full enrichment")
        if not enriched_universe_rows:
            enriched_universe_rows = universe_rows[: min(len(universe_rows), settings.enrichment_limit)]
            warnings.append("Prefilter returned no rows; fell back to the first slice of universe rows.")

        candidate_rows: List[Dict[str, Any]] = []
        feature_rows: List[Dict[str, Any]] = []
        total = len(enriched_universe_rows)
        _update_status(phase="enriching_tickers", message="Fetching fundamentals and news", progress_current=0, progress_total=total)

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
                _update_status(
                    phase="enriching_tickers",
                    message=f"Enriched {row['symbol']}",
                    progress_current=index,
                    progress_total=total,
                )

        for index, row in enumerate(enriched_universe_rows, start=1):
            ticker = row["symbol"]
            bundle = bundles.get(ticker) or TickerDataBundle(ticker=ticker, warnings=["No bundle returned"])
            bundle.company_name = bundle.company_name or row.get("name", ticker)
            bundle.sector = bundle.sector or row.get("sector", "")
            price_history = bundle.price_history if bundle.price_history is not None and not bundle.price_history.empty else history_map.get(ticker)
            sector_etf = SECTOR_ETF_MAP.get(bundle.sector or row.get("sector", ""))
            sector_history = history_map.get(sector_etf) if sector_etf else None

            timing_metrics = compute_timing_metrics(price_history, benchmark_spy, sector_history)
            timing_score, timing_reasons, timing_risks, technical_summary = score_timing(timing_metrics)
            structural_score, structural_reasons, structural_risks, fundamental_summary = score_structural(bundle)
            catalyst_score, catalyst_reasons, catalyst_risks, catalyst_metrics, prepared_news = score_catalyst(bundle.news)

            combined_reasons = list(dict.fromkeys(structural_reasons + catalyst_reasons + timing_reasons))
            combined_risks = list(dict.fromkeys(bundle.warnings + structural_risks + catalyst_risks + timing_risks))
            overall_score = round(
                structural_score * settings.structural_weight
                + catalyst_score * settings.catalyst_weight
                + timing_score * settings.timing_weight,
                2,
            )
            candidate_rows.append(
                {
                    "run_id": run_id,
                    "rank": 0,
                    "ticker": ticker,
                    "company_name": bundle.company_name,
                    "sector": bundle.sector,
                    "industry": bundle.industry,
                    "overall_score": overall_score,
                    "structural_score": structural_score,
                    "catalyst_score": catalyst_score,
                    "timing_score": timing_score,
                    "confidence_band": confidence_band(overall_score),
                    "reason_codes_json": json.dumps(combined_reasons[:6]),
                    "risk_flags_json": json.dumps(combined_risks[:6]),
                    "latest_news_json": json.dumps(prepared_news[:5]),
                    "technical_summary": technical_summary,
                    "fundamental_summary": fundamental_summary,
                    "feature_snapshot_json": json.dumps({
                        "timing": timing_metrics,
                        "fundamentals": bundle.fundamentals,
                        "catalyst": catalyst_metrics,
                    }, default=str),
                }
            )
            feature_rows.append(
                {
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
                    "risk_flags": " | ".join(combined_risks[:6]),
                    "reason_codes": " | ".join(combined_reasons[:6]),
                }
            )
            _update_status(
                phase="scoring",
                message=f"Scored {ticker}",
                progress_current=index,
                progress_total=total,
            )

        ranked_rows = sorted(candidate_rows, key=lambda x: x["overall_score"], reverse=True)
        for idx, row in enumerate(ranked_rows, start=1):
            row["rank"] = idx
        replace_candidates(run_id, ranked_rows)
        shortlist_rows = ranked_rows[: settings.shortlist_size]

        scan_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "ended_at": utc_now_iso(),
            "provider": provider.provider_name,
            "universe_name": settings.default_universe_name,
            "universe_size_loaded": len(universe_rows),
            "bulk_price_ticker_count": len(bulk_tickers),
            "enrichment_size": len(enriched_universe_rows),
            "shortlist_size": len(shortlist_rows),
            "score_weights": {
                "structural": settings.structural_weight,
                "catalyst": settings.catalyst_weight,
                "timing": settings.timing_weight,
            },
            "note": "Version 1 outputs opportunity scores, not calibrated probabilities.",
            "warnings": warnings,
        }

        reasons_by_ticker = {
            row["ticker"]: {
                "reason_codes": json.loads(row["reason_codes_json"]),
                "risk_flags": json.loads(row["risk_flags_json"]),
            }
            for row in ranked_rows
        }
        ranked_json_rows = []
        for row in ranked_rows:
            converted = dict(row)
            converted["reason_codes"] = json.loads(converted.pop("reason_codes_json"))
            converted["risk_flags"] = json.loads(converted.pop("risk_flags_json"))
            converted["latest_news"] = json.loads(converted.pop("latest_news_json"))
            converted["feature_snapshot"] = json.loads(converted.pop("feature_snapshot_json"))
            ranked_json_rows.append(converted)

        write_json(run_dir / "scan_summary.json", scan_summary)
        write_csv(run_dir / "ranked_candidates.csv", [{k: v for k, v in row.items() if not k.endswith("_json")} for row in ranked_rows])
        write_json(run_dir / "ranked_candidates.json", ranked_json_rows)
        write_json(run_dir / "reasons_by_ticker.json", reasons_by_ticker)
        write_csv(run_dir / "raw_or_normalized_feature_snapshot.csv", feature_rows)
        write_json(run_dir / "config_used.json", settings.to_dict())
        write_text(run_dir / "scan_log.txt", "\n".join(log_lines + [f"[{utc_now_iso()}] Completed successfully"]))
        zip_path = run_dir / f"{run_id}_scan_pack.zip"
        zip_directory(run_dir, zip_path)

        ended_at = utc_now_iso()
        upsert_run(
            _build_run_record(
                run_id,
                settings,
                "completed",
                started_at,
                message="Scan completed",
                ended_at=ended_at,
                progress_current=total,
                progress_total=total,
                phase="completed",
                universe_size=len(universe_rows),
                enrichment_size=len(enriched_universe_rows),
                shortlist_size=len(shortlist_rows),
                warnings=warnings,
                artifacts_dir=str(run_dir),
                artifact_zip_path=str(zip_path),
                summary=scan_summary,
            )
        )
        _update_status(
            is_running=False,
            run_id=run_id,
            phase="completed",
            message="Scan completed",
            progress_current=total,
            progress_total=total,
        )
    except Exception as exc:
        log_lines.append(f"[{utc_now_iso()}] FAILED: {exc}")
        write_text(run_dir / "scan_log.txt", "\n".join(log_lines))
        ended_at = utc_now_iso()
        upsert_run(
            _build_run_record(
                run_id,
                settings,
                "failed",
                started_at,
                message=str(exc),
                ended_at=ended_at,
                phase="failed",
                warnings=warnings + [str(exc)],
                artifacts_dir=str(run_dir),
            )
        )
        _update_status(
            is_running=False,
            run_id=run_id,
            phase="failed",
            message=str(exc),
            progress_current=0,
            progress_total=0,
        )



def latest_run_with_candidates() -> Optional[Dict[str, Any]]:
    runs = list_runs(limit=1)
    if not runs:
        return None
    run = deserialize_run(runs[0])
    if run:
        run["candidates"] = [deserialize_candidate(row) for row in list_candidates(run["run_id"])]
    return run
