# Stock Scanner V1 — Claude Fix Pack Applied

This bundle starts from `stock_scanner_v1_render_hotfix.zip` and additionally applies the reviewed fix pack.

Applied fixes:
- Reordered download routes so `/download/run/{run_id}/scan-pack` is no longer shadowed.
- Closed the missing `</section>` tag in `scanner.html`.
- Moved scan cooldown enforcement inside the lock and set `is_running=True` before starting the scan thread.
- Applied ISO-string news timestamp cutoff parsing in the yfinance provider.
- Prevented negative book equity from being treated as a positive balance-sheet signal.
- Added a 7-day universe cache TTL and stale-cache fallback.
- Replaced brittle `XL*` exclusion with an explicit benchmark ETF set.
- Made runtime status persistence non-fatal if disk writes fail.

Smoke-tested locally in demo mode:
- `/scanner` 200
- `/latest-results` 200
- `/runs` 200
- `/settings` 200
- `/status` 200
- `POST /api/scan/run` starts successfully
- `/download/run/{run_id}/scan-pack` returns 200 `application/zip`
