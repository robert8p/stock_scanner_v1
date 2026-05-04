# V1.2 Fix Brief

## Why this tranche exists
V1.1 fixed the major mechanical issues, but live evidence still showed three material weaknesses:
- scan breadth was still too narrow in practice
- catalyst scoring still let weak/generic finance content influence rankings too much
- operator review still lacked explicit funnel diagnostics and output hygiene

## What V1.2 changes

### 1. Broad-universe scan reliability
- default scan breadth remains aligned to the S&P 500 universe
- yfinance bulk history downloads are now chunked in batches instead of relying on one large call
- if the service is still using the prior narrow V1.1 defaults of `SCAN_TICKER_LIMIT=120` and `ENRICHMENT_LIMIT=60` **without a saved UI override**, V1.2 auto-upgrades those to `500` and `120`

### 2. Funnel diagnostics
Every run now writes `coverage_diagnostics.json` and expands `scan_summary.json` with:
- universe available
- universe loaded
- price-history coverage
- enrichment selected
- bundle count
- ranked-after-dedupe count
- shortlist size
- per-feature coverage percentages for core structural, catalyst, and timing inputs

### 3. Tighter catalyst relevance
The catalyst scorer now:
- requires materially stronger ticker/company relevance
- suppresses generic market-roundup content harder
- downweights lower-signal publishers
- elevates higher-credibility relevant headlines
- records filtered generic/irrelevant counts in the feature snapshot

### 4. Output hygiene
- `risk_flags` now writes `None identified` instead of collapsing to blank/NaN-style review output
- CSV/JSON artifact writing sanitizes NaN/inf values more consistently
- settings update API returns redacted settings

### 5. UI/operator improvements
- Scanner and Latest Results pages now surface funnel counts directly
- Runs page now links the coverage diagnostics artifact
- Settings page now explains the recommended V1.2 breadth settings

## What V1.2 is meant to prove
- the app can scan broadly enough to act like a real market scanner rather than a narrow slice scorer
- the operator can see where names are being lost in the funnel
- catalyst inputs are meaningfully cleaner before any V2 calibration work begins

## What V1.2 still does not prove
- calibrated probabilities
- out-of-sample historical edge
- live monitoring / alerts / drift controls

Those remain V2/V3 work only.
