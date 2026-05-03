from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_settings, persist_settings
from .db import deserialize_candidate, deserialize_run, get_candidate, get_latest_run, get_run, init_db, list_candidates, list_runs
from .scanner import ScanAlreadyRunningError, ScanCooldownError, get_runtime_status, latest_run_with_candidates, run_scan_now
from .universe import load_universe

settings = load_settings()
settings.ensure_paths()
init_db()

app = FastAPI(title=settings.app_name)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/scanner", status_code=302)


@app.get("/health")
def health() -> Dict[str, Any]:
    current_settings = load_settings()
    latest = deserialize_run(get_latest_run())
    return {
        "status": "ok",
        "app_name": current_settings.app_name,
        "provider": "demo" if current_settings.demo_mode else current_settings.default_provider,
        "data_dir": current_settings.data_dir,
        "database_path": current_settings.database_path,
        "artifacts_dir": current_settings.artifacts_dir,
        "latest_run": latest,
    }


@app.get("/scanner", response_class=HTMLResponse)
def scanner_page(request: Request):
    latest = latest_run_with_candidates()
    return templates.TemplateResponse(
        "scanner.html",
        {"request": request, "settings": load_settings(), "latest": latest, "runtime_status": get_runtime_status()},
    )


@app.get("/latest-results", response_class=HTMLResponse)
def latest_results_page(request: Request):
    latest = latest_run_with_candidates()
    return templates.TemplateResponse(
        "latest_results.html",
        {"request": request, "latest": latest, "runtime_status": get_runtime_status()},
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
        "candidate_detail.html",
        {"request": request, "run": run, "candidate": candidate},
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    runs = [deserialize_run(run) for run in list_runs(limit=50)]
    return templates.TemplateResponse("runs.html", {"request": request, "runs": runs})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "settings": load_settings()},
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
    return templates.TemplateResponse(
        "status.html",
        {"request": request, "health": health(), "runtime_status": get_runtime_status()},
    )


@app.get("/api/status")
def api_status():
    return JSONResponse(get_runtime_status())


@app.get("/api/universe")
def api_universe():
    rows = load_universe()
    return {"universe_name": load_settings().default_universe_name, "size": len(rows), "rows": rows[:200]}


@app.post("/api/scan/run")
def api_scan_run():
    try:
        run_id = run_scan_now()
        return {"status": "started", "run_id": run_id}
    except (ScanAlreadyRunningError, ScanCooldownError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/scan/latest")
def api_scan_latest():
    latest = latest_run_with_candidates()
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


@app.get("/api/artifacts")
def api_artifacts(limit: int = 20):
    runs = [deserialize_run(run) for run in list_runs(limit=limit)]
    return {"runs": runs}


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


@app.get("/download/run/{run_id}/scan-pack")
def download_scan_pack(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    zip_path = Path(run.get("artifact_zip_path") or "")
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Scan pack not found")
    return FileResponse(zip_path, filename=zip_path.name)


@app.get("/api/settings")
def api_settings():
    current = load_settings()
    return current.to_dict()


@app.post("/api/settings/update")
async def api_settings_update(request: Request):
    payload = await request.json()
    updated = persist_settings(payload)
    return {"status": "updated", "settings": updated.to_dict()}
