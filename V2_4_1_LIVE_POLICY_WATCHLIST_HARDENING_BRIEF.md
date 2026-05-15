# V2.4.1 Live Policy Watchlist Hardening

## Purpose

V2.4 proved the live elite-policy overlay could find raw matches, but the live risk gate rejected every match. That made the overlay technically honest but operationally too binary: useful policy resemblances disappeared into rejection instead of being surfaced as watchlist candidates.

V2.4.1 keeps calibrated probabilities off and improves operator usability by splitting live policy output into explicit categories.

## What changed

1. **Policy eligible**
   - Candidate matches at least one validated elite replay policy.
   - At least one matched policy passes the configured stop-rate gate.
   - No hard live data-quality / evidence-completeness risk blocks eligibility.

2. **Policy watchlist — elevated stop risk**
   - Candidate matches validated elite replay policies.
   - Matched policy evidence exists, but the matched policies fail the strict stop-rate gate.
   - Candidate is useful for manual review, but not eligible.

3. **Policy watchlist — live risk gated**
   - Candidate matches validated elite replay policies.
   - Hard live data/evidence/risk checks block eligibility.

4. **Policy watchlist resemblance**
   - Candidate only matches watchlist-level replay policies.

5. **No validated elite policy**
   - No replay-validated policy resemblance.

## Artifact changes

The scan pack now includes:

- `policy_eligible_candidates.csv`
- `policy_watchlist_candidates.csv`
- `live_policy_overlay_report.json`

The overlay report now includes:

- `eligible_count`
- `policy_watchlist_candidate_count`
- `elevated_stop_watchlist_count`
- `hard_risk_rejected_count`
- `eligible_policy_match_counts`
- `elevated_stop_policy_match_counts`

## Important truth boundary

This build still does **not** enable calibrated probability display.

A policy watchlist candidate is not a buy signal. It means the candidate resembles a historically useful replay slice but carries elevated stop risk, hard live data-quality risk, or watchlist-only status.

## Validation performed before packaging

- Python compile check passed.
- Main pages rendered locally.
- Replay page rendered locally.
- Demo scan completed end to end.
- Scan pack downloaded successfully.
- Artifact integrity passed.
- New `policy_watchlist_candidates.csv` artifact was present in the scan pack.

## Retest steps

1. Deploy the full ZIP.
2. Confirm the app header shows:
   - `v2.4.1`
   - `v2.4.1-live-policy-watchlist-hardening`
3. Run one live scan.
4. Download the scan pack.
5. Upload back:
   - latest `*_scan_pack.zip`
   - `live_policy_overlay_report.json`
   - `policy_eligible_candidates.csv`
   - `policy_watchlist_candidates.csv`
   - `score_diagnostics.json`
   - `coverage_diagnostics.json`
   - `health.json`
   - `status.json`
