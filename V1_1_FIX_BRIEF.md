# V1.1 Fix Brief — News + Fundamentals + Technicals Stock Scanner

## Why V1.1 exists

The first deployed V1 proved the app could run, but the returned scan pack showed six hard issues that needed to be corrected before any V2 work:

1. Secrets were leaking into `config_used.json` and persisted run metadata.
2. The scan only covered 50 names because the universe fallback was too small when live scraping failed.
3. The catalyst score was too vulnerable to generic market headlines.
4. Duplicate share classes like GOOG and GOOGL could consume separate shortlist slots.
5. Sector-relative timing metrics were blank too often because provider sector names did not map cleanly to the ETF lookup table.
6. The ranked CSV was too thin for operator review because the best explanations lived mainly in JSON.

## Exact V1.1 corrections

### 1) Secret redaction
- Redact API keys and secrets before writing `config_used.json`.
- Redact secrets before persisting `settings_json` into the `runs` table.
- Redact secrets from status and health responses.

### 2) Universe reliability
- Keep Wikipedia scraping as the preferred live source.
- Add a bundled S&P 500 constituent CSV fallback inside the app.
- Cache the bundled/live result to disk after load.
- Raise the default scan cap to 500 so the intended breadth is actually scanned.

### 3) Ticker-specific news gating
- Add related-ticker support to normalized news items.
- Only count headlines that are likely relevant to the ticker using ticker mention, related tickers, or company-name token matches.
- Penalize generic market-wrap headlines so they do not inflate catalyst scores across many names.

### 4) Share-class dedupe
- Deduplicate rows representing the same underlying company using CIK first and normalized company name second.
- Keep only the highest-scoring line where duplicate share classes exist.
- Log the dedupe actions and surface them in warnings/logs.

### 5) Sector normalization
- Map common provider sector labels such as `Technology`, `Healthcare`, `Consumer Cyclical`, `Consumer Defensive`, `Financial Services`, and `Basic Materials` to the ETF-sector scheme used by the timing model.
- This restores sector-relative strength for a much larger share of rows.

### 6) Richer operator outputs
- Put reason codes, risk flags, and top news titles directly into `ranked_candidates.csv`.
- Keep the richer JSON artifact too, but make the CSV materially more review-friendly.

## Acceptance bar for V1.1

V1.1 should only be considered ready if the next scan pack shows all of the following:

- no secrets exposed in any artifact or status output
- universe size materially larger than 50 and aligned to the intended S&P 500 breadth
- duplicate share classes no longer wasting shortlist slots
- sector-relative strength populated much more consistently
- ranked CSV itself is sufficient for a first-pass operator review
- catalyst headlines feel more ticker-specific and less like recycled market commentary
