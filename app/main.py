from __future__ import annotations

import json
from pathlib import Path
import zipfile
from typing import Any, Dict

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_settings, persist_settings
from .db import (
    deserialize_candidate,
    deserialize_run,
    get_candidate,
    get_latest_run,
    get_run,
    init_db,
    list_candidates,
    list_runs,
    list_shortlist_outcomes,
    summarize_shortlist_outcomes,
)
from .scanner import (
    SHORTLIST_DEFAULT_SORT_MODE,
    SORT_MODE_LABELS,
    ScanAlreadyRunningError,
    ScanCooldownError,
    get_runtime_status,
    latest_run_with_candidates,
    normalize_sort_mode,
    run_scan_now,
    sanitize_settings_payload,
)
from .storage import zip_directory
from .universe import load_universe

settings = load_settings()
settings.ensure_paths()
init_db()

app = FastAPI(title=settings.app_name)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


REQUIRED_SCAN_PACK_ARTIFACTS = {
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
}


def _build_info() -> Dict[str, Any]:
    current = load_settings()
    return {
        "app_version": current.app_version,
        "build_id": current.build_id,
        "build_timestamp_utc": current.build_timestamp_utc,
        "artifact_schema_version": current.artifact_schema_version,
    }


def _read_artifact_integrity(run: Dict[str, Any] | None) -> Dict[str, Any]:
    if not run:
        return {"status": "unknown", "issues": ["No latest run available"]}
    artifacts_dir = Path(run.get("artifacts_dir") or "")
    manifest_path = artifacts_dir / "artifact_manifest.json"
    if not manifest_path.exists():
        return {"status": "missing", "issues": ["artifact_manifest.json missing"]}
    try:
        payload = json.loads(manifest_path.read_text())
    except Exception as exc:
        return {"status": "invalid", "issues": [f"Could not parse artifact_manifest.json: {exc}"]}
    integrity = payload.get("artifact_integrity") or {"status": "unknown", "issues": ["artifact_integrity block missing"]}
    integrity["artifacts_present"] = payload.get("artifacts_present_before_zip", [])
    integrity["required_artifacts"] = payload.get("required_artifacts", [])
    return integrity


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/scanner", status_code=302)


@app.get("/health")
def health() -> Dict[str, Any]:
    current_settings = load_settings()
    latest = deserialize_run(get_latest_run())
    latest_safe = latest.copy() if latest else None
    if latest_safe and isinstance(latest_safe.get("settings_json"), str):
        latest_safe["settings_json"] = "***REDACTED***"
    if latest_safe and isinstance(latest_safe.get("settings"), dict):
        latest_safe["settings"] = sanitize_settings_payload(latest_safe["settings"])
    return {
        "status": "ok",
        "app_name": current_settings.app_name,
        "build": _build_info(),
        "provider": "demo" if current_settings.demo_mode else current_settings.default_provider,
        "data_dir": current_settings.data_dir,
        "database_path": current_settings.database_path,
        "artifacts_dir": current_settings.artifacts_dir,
        "settings": sanitize_settings_payload(current_settings.to_dict()),
        "latest_run": latest_safe,
        "artifact_integrity": _read_artifact_integrity(latest or None),
        "outcome_tracking_summary": summarize_shortlist_outcomes(),
    }


def _json_download(payload: Dict[str, Any], filename: str):
    tmp_dir = Path(load_settings().artifacts_dir) / "_downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / filename
    path.write_text(json.dumps(payload, indent=2, default=str))
    return FileResponse(path, filename=filename, media_type="application/json")


def _ensure_scan_pack_parity(run: Dict[str, Any]) -> Path:
    artifacts_dir = Path(run.get("artifacts_dir") or "")
    zip_path = Path(run.get("artifact_zip_path") or "")
    if not artifacts_dir.exists():
        raise HTTPException(status_code=404, detail="Artifacts directory not found")

    existing_files = {p.name for p in artifacts_dir.iterdir() if p.is_file()}
    missing_on_disk = sorted(REQUIRED_SCAN_PACK_ARTIFACTS - existing_files)
    if missing_on_disk:
        raise HTTPException(status_code=409, detail=f"Required artifacts missing on disk: {', '.join(missing_on_disk)}")

    zip_names = set()
    if zip_path.exists():
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zip_names = {Path(name).name for name in zf.namelist() if not name.endswith("/")}
        except Exception:
            zip_names = set()

    if (not zip_path.exists()) or (REQUIRED_SCAN_PACK_ARTIFACTS - zip_names):
        zip_directory(artifacts_dir, zip_path)

    return zip_path


@app.get("/scanner", response_class=HTMLResponse)
def scanner_page(request: Request):
    latest = latest_run_with_candidates(sort_mode=SHORTLIST_DEFAULT_SORT_MODE)
    return templates.TemplateResponse(
        request=request,
        name="scanner.html",
        context={
            "request": request,
            "settings": load_settings(),
            "latest": latest,
            "runtime_status": get_runtime_status(),
            "build": _build_info(),
        },
    )


@app.get("/latest-results", response_class=HTMLResponse)
def latest_results_page(request: Request, sort_mode: str = SHORTLIST_DEFAULT_SORT_MODE):
    resolved_sort_mode = normalize_sort_mode(sort_mode)
    latest = latest_run_with_candidates(sort_mode=resolved_sort_mode)
    sort_options = [{"value": value, "label": label} for value, label in SORT_MODE_LABELS.items()]
    return templates.TemplateResponse(
        request=request,
        name="latest_results.html",
        context={
            "request": request,
            "latest": latest,
            "runtime_status": get_runtime_status(),
            "sort_mode": resolved_sort_mode,
            "sort_options": sort_options,
            "build": _build_info(),
        },
    )


@app.get("/candidate/{ticker}", response_class=HTMLResponse)
def candidate_page(request: Request, ticker: str, run_id: str | None = None):
    run = deserialize_run(get_run(run_id)) if run_id else deserialize_run(get_latest_run())
    if not run:
        raise HTTPException(status_code=404, detail="No runs available")
    candidate = deserialize_candidate(get_candidate(run["run_id"], ticker.upper()))
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return templates.TemplateResponse(
        request=request,
        name="candidate_detail.html",
        context={"request": request, "run": run, "candidate": candidate, "build": _build_info()},
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    runs = [deserialize_run(run) for run in list_runs(limit=50)]
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={"request": request, "runs": runs, "build": _build_info()},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"request": request, "settings": load_settings(), "build": _build_info()},
    )


@app.post("/settings", response_class=HTMLResponse)
def update_settings_page(
    request: Request,
    default_provider: str = Form(...),
    demo_mode: str = Form("false"),
    scan_ticker_limit: int = Form(...),
    enrichment_limit: int = Form(...),
    shortlist_size: int = Form(...),
    lookback_days: int = Form(...),
    news_lookback_days: int = Form(...),
    max_workers: int = Form(...),
    structural_weight: float = Form(...),
    catalyst_weight: float = Form(...),
    timing_weight: float = Form(...),
):
    persist_settings(
        {
            "default_provider": default_provider,
            "demo_mode": demo_mode,
            "scan_ticker_limit": scan_ticker_limit,
            "enrichment_limit": enrichment_limit,
            "shortlist_size": shortlist_size,
            "lookback_days": lookback_days,
            "news_lookback_days": news_lookback_days,
            "max_workers": max_workers,
            "structural_weight": structural_weight,
            "catalyst_weight": catalyst_weight,
            "timing_weight": timing_weight,
        }
    )
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    latest = deserialize_run(get_latest_run())
    return templates.TemplateResponse(
        request=request,
        name="status.html",
        context={
            "request": request,
            "health": health(),
            "runtime_status": get_runtime_status(),
            "artifact_integrity": _read_artifact_integrity(latest or None),
            "recent_outcomes": list_shortlist_outcomes(limit=25),
            "build": _build_info(),
        },
    )


@app.get("/download/health")
def download_health():
    return _json_download(health(), "health.json")


@app.get("/download/status")
def download_status():
    return _json_download(get_runtime_status(), "status.json")


@app.get("/api/status")
def api_status():
    return JSONResponse(get_runtime_status())


@app.get("/api/universe")
def api_universe():
    rows = load_universe()
    return {"universe_name": load_settings().default_universe_name, "size": len(rows), "rows": rows[:200]}


@app.post("/api/scan/run")
async def api_scan_run(request: Request, x_idempotency_key: str | None = Header(default=None)):
    try:
        payload = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = await request.json()
        request_key = x_idempotency_key or payload.get("request_key")
        run_id = run_scan_now(request_key=request_key)
        return {"status": "started", "run_id": run_id, "request_key": request_key}
    except (ScanAlreadyRunningError, ScanCooldownError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/scan/latest")
def api_scan_latest(sort_mode: str = SHORTLIST_DEFAULT_SORT_MODE):
    latest = latest_run_with_candidates(sort_mode=sort_mode)
    if not latest:
        raise HTTPException(status_code=404, detail="No runs available")
    return latest


@app.get("/api/scan/history")
def api_scan_history(limit: int = 20):
    return {"runs": [deserialize_run(run) for run in list_runs(limit=limit)]}


@app.get("/api/candidate/{ticker}")
def api_candidate(ticker: str, run_id: str | None = None):
    run = deserialize_run(get_run(run_id)) if run_id else deserialize_run(get_latest_run())
    if not run:
        raise HTTPException(status_code=404, detail="No runs available")
    candidate = deserialize_candidate(get_candidate(run["run_id"], ticker.upper()))
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@app.get("/api/outcomes")
def api_outcomes(limit: int = 100, status: str | None = None):
    return {
        "summary": summarize_shortlist_outcomes(),
        "rows": list_shortlist_outcomes(limit=limit, status=status),
        "build": _build_info(),
    }


@app.get("/api/artifacts")
def api_artifacts(limit: int = 20):
    runs = [deserialize_run(run) for run in list_runs(limit=limit)]
    return {"runs": runs, "build": _build_info()}


@app.get("/download/run/{run_id}/scan-pack")
def download_scan_pack(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    zip_path = _ensure_scan_pack_parity(run)
    return FileResponse(zip_path, filename=zip_path.name)


@app.get("/download/run/{run_id}/{filename}")
def download_artifact(run_id: str, filename: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    artifacts_dir = Path(run["artifacts_dir"])
    target = artifacts_dir / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/settings")
def api_settings():
    current = load_settings()
    return {"build": _build_info(), "settings": sanitize_settings_payload(current.to_dict())}


@app.post("/api/settings/update")
async def api_settings_update(request: Request):
    payload = await request.json()
    updated = persist_settings(payload)
    return {"status": "updated", "build": _build_info(), "settings": sanitize_settings_payload(updated.to_dict())}
