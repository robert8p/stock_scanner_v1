# V1.5 Fix Brief — Shortlist Truth + Rank Semantics Hardening

## Objective
Fix the remaining V1 blindspot: the app now tells the truth about catalyst quality, but the **Latest Results** page still placed unlike concepts on one flat ladder. V1.5 changes the operator presentation so catalyst-backed names are not visually buried under stronger quality/momentum names.

## What changed

### 1) New display sort modes on Latest Results
Added three operator-selectable views:
- **Catalyst-backed first** — default view
- **All ranked (score order)**
- **Quality/momentum first**

These change the **display order only**. They do **not** claim a new calibrated probability or replace the underlying score logic.

### 2) Default grouped shortlist view
The default Latest Results view now groups names into:
- Catalyst-backed
- Catalyst-supported
- Quality/momentum · mixed catalyst
- Quality/momentum · weak catalyst

This makes the shortlist semantically honest by default.

### 3) Score rank remains visible
Each row now shows:
- **View rank**
- **Score rank**

This preserves transparency. Operators can see the difference between the raw score ladder and the truth-prioritized display order.

### 4) New scan-pack artifact
Added:
- `shortlist_views.json`

This contains the top shortlist under each display mode:
- `score`
- `catalyst_first`
- `quality_first`

### 5) Diagnostics now expose view distributions
Added shortlist-view distributions to:
- `score_diagnostics.json`
- `coverage_diagnostics.json`
- `scan_summary.json`

### 6) Existing Health / Status download buttons remain
The Health / Status page continues to expose:
- Download health
- Download status

## What this tranche does **not** do
- It does **not** change the 120 enrichment gate.
- It does **not** introduce historical calibration.
- It does **not** claim probabilities.
- It does **not** change the underlying composite-score formula beyond the existing V1.4 truth penalties.

## Why this tranche is the right next move
The latest live evidence showed:
- breadth is now good enough for V1
- timing saturation is no longer the main issue
- catalyst truth is now surfaced honestly
- but the operator still sees unlike opportunity types on one flat ladder

So the next best move was a **presentation and operator-truth tranche**, not V2.

## Validation completed before packaging
- Python compile check passed
- demo-mode scan completed end to end
- Latest Results page rendered in all three sort modes
- grouped view rendered correctly
- `shortlist_views.json` was created in the artifact pack
- scan-pack download route still worked
- Health / Status page still rendered and downloads remained available

## Redeploy notes
No new environment variables are required.

Keep the current V1.4/V1.2 settings, especially:
- `SCAN_TICKER_LIMIT=500`
- `ENRICHMENT_LIMIT=120`
- `YFINANCE_BULK_CHUNK_SIZE=100`

## Retest steps
1. Redeploy this ZIP.
2. Open **Latest Results**.
3. Check the three sort modes:
   - Catalyst-backed first
   - All ranked (score order)
   - Quality/momentum first
4. Confirm grouped sections appear in the non-flat views.
5. Confirm each row shows both **View rank** and **Score rank**.
6. Download the scan pack.
7. Confirm the scan pack contains `shortlist_views.json`.
8. Confirm `score_diagnostics.json` and `coverage_diagnostics.json` both include shortlist-view distributions.
9. Confirm **Health / Status** downloads still work.

## Upload back after testing
Please upload back:
- the latest `*_scan_pack.zip`
- `shortlist_views.json`
- `score_diagnostics.json`
- `coverage_diagnostics.json`
- `Health.txt`
- `Status.txt`

## Decision gate after retest
If V1.5 makes the shortlist materially easier to interpret and compare honestly, then the next decision is whether to:
- do one more V1 tranche on catalyst quality itself, or
- move to V2 replay/calibration work

That decision should be made from the next live evidence, not assumed in advance.
