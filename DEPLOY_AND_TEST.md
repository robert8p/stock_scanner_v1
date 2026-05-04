# Deploy and Test — Stock Scanner V1.1

## Architecture

- **Backend:** FastAPI
- **Frontend:** server-rendered Jinja templates plus lightweight polling JS
- **Persistence:** SQLite for runs/candidates + artifact files on persistent disk
- **Deployment target:** Render single web service
- **Runtime model:** one worker, on-demand background scan thread, operator-visible progress polling
- **Provider setup:** working `yfinance` path for live data plus `DEMO_MODE=true` synthetic mode for deployment smoke tests
- **Universe reliability:** live Wikipedia scrape preferred, bundled S&P 500 CSV fallback included so the universe does not collapse when scraping fails

## Required environment variables

These are the exact variables the app understands. Only the first block is effectively required for standard deployment.

### Core runtime
- `APP_ENV=production`
- `DATA_DIR=/var/data`
- `DATABASE_PATH=/var/data/scanner.db`
- `ARTIFACTS_DIR=/var/data/artifacts`
- `SETTINGS_PATH=/var/data/settings.json`
- `RUNTIME_STATUS_PATH=/var/data/runtime_status.json`
- `UNIVERSE_CACHE_PATH=/var/data/universe_cache.json`
- `DATA_PROVIDER=yfinance`
- `DEMO_MODE=false`

### Scan behaviour
- `SCAN_TICKER_LIMIT=500`
- `ENRICHMENT_LIMIT=120`
- `SHORTLIST_SIZE=20`
- `LOOKBACK_DAYS=320`
- `NEWS_LOOKBACK_DAYS=7`
- `MAX_WORKERS=8`
- `STRUCTURAL_WEIGHT=0.35`
- `CATALYST_WEIGHT=0.30`
- `TIMING_WEIGHT=0.35`
- `SCAN_COOLDOWN_SECONDS=5`

### Optional future-provider placeholders
- `FINNHUB_API_KEY=`
- `POLYGON_API_KEY=`
- `ALPACA_API_KEY=`
- `ALPACA_API_SECRET=`
- `ALPACA_BASE_URL=https://data.alpaca.markets`

## Render deployment steps

1. Upload this full app folder to a GitHub repo or upload the ZIP contents into your deployment workflow.
2. In Render, create a **Web Service**.
3. Use:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
4. Attach a persistent disk mounted at `/var/data`.
5. Add the environment variables from the list above.
6. Deploy.

## First deployment smoke test

If you want to prove the app plumbing before hitting live market/news endpoints:

1. Set `DEMO_MODE=true`
2. Deploy or redeploy.
3. Open `/scanner`
4. Click **Run Scan**
5. Confirm:
   - progress moves
   - latest results populate
   - candidate detail pages open
   - scan pack downloads

After that, set `DEMO_MODE=false` and redeploy for a live-data test.

## Live-data V1.1 test steps

1. Open `/scanner`
2. Click **Run Scan**
3. Wait for completion on the same page
4. Open **Latest Results**
5. Open at least 3 candidate detail pages
6. Open **Runs / Artifacts**
7. Download the latest scan pack ZIP
8. Open `/health`
9. Open `/api/status`
10. Confirm in the downloaded scan pack that:
   - `config_used.json` shows redacted secrets only
   - `ranked_candidates.csv` includes reason codes, risk flags, and top news titles
   - `scan_summary.json` shows a materially broader universe than the earlier 50-name fallback run

## What to upload back after testing

Please upload back exactly these items:

1. The downloaded latest `*_scan_pack.zip`
2. A screenshot of `/latest-results`
3. A screenshot of one `/candidate/{ticker}` detail page
4. The `/health` JSON output
5. The `/api/status` JSON output
6. Any visible error message or confusing behaviour you notice

## What V1.1 proves

- the app can run a transparent, explainable ranking scan
- scores are decomposed into structural, catalyst, and timing components
- per-run artifacts exist and are downloadable
- operator status is surfaced clearly
- the universe is resilient to live scraping failures
- secrets are no longer written into artifacts/status payloads

## What V1.1 does **not** prove

- calibrated probability quality
- that the score ranking has historical edge
- that the current scoring logic beats naive alternatives out of sample
- live scheduling / drift handling / alerts

Those still belong to Version 2 and Version 3.


## V1.2 redeploy / retest notes

Use or confirm these values in Render for the intended broad V1.2 scan:
- `SCAN_TICKER_LIMIT=500`
- `ENRICHMENT_LIMIT=120`
- `YFINANCE_BULK_CHUNK_SIZE=100`

After redeploy:
1. Open `/settings` and confirm the scan cap shows `500` and enrichment cap shows `120`.
2. Run one live scan from `/scanner`.
3. Check `/latest-results` for the funnel counts row.
4. Download both the scan pack and `coverage_diagnostics.json`.
5. Upload back:
   - the new `*_scan_pack.zip`
   - `coverage_diagnostics.json`
   - `/health`
   - `/api/status`

Pass condition for this tranche:
- universe loaded is near full S&P 500 breadth
- price-history coverage is high enough that enrichment is being chosen from a broad base
- news quality in top names looks cleaner and less generic
- no secrets leak and no NaN-style review glitches remain
