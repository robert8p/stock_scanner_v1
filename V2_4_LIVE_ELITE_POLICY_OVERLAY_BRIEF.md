# V2.4 — Live elite-policy overlay + risk-adjusted policy hardening

## Objective
Bridge the V2.3 replay evidence into the live scanner without enabling false precision.

V2.4 does **not** display calibrated probabilities. It adds live policy-resemblance badges that show whether a candidate resembles historically stronger replay slices such as top-3/top-5, score-threshold, regime, and surface-policy gates.

## What changed

### 1. Live replay-surface score
Each live candidate now receives a separate replay-style score derived from the timing replay surface:

- `replay_surface_score`
- `replay_surface_rank`
- `replay_surface_label` (`continuation`, `rebound`, or `blended`)
- `market_regime` (`risk_on`, `neutral`, `risk_off`, or `unknown`)

This score is separate from the live composite opportunity score.

### 2. Live elite-policy overlay
The live scanner reads the latest completed replay's `elite_policy_leaderboard.csv` and uses only policies marked `pass` for live validated-policy badges.

The live scanner now adds:

- `live_policy_label`
- `live_policy_badges`
- `live_policy_ids`
- `live_policy_watchlist_ids`
- `live_policy_warning`
- `live_policy_risk_flags`
- `live_policy_match_count`
- `live_policy_risk_adjusted_match_count`
- `live_policy_reference_stop_rate`

### 3. Risk-adjusted policy hardening
A candidate can resemble a validated replay policy but still be rejected by live risk gates.

Risk checks include:

- thin evidence quality
- evidence completeness below threshold
- stale or incomplete data warnings
- stretched RSI / extension / weak trend flags
- historical policy stop rate above the live overlay risk threshold

### 4. New scan artifacts
Every scan pack now includes:

- `live_policy_overlay_report.json`
- `policy_eligible_candidates.csv`

The scan pack integrity check now requires these artifacts.

### 5. UI additions
Latest Results and Candidate Detail now show:

- replay-surface score and rank
- surface label
- market regime
- live elite-policy overlay label
- policy badges
- policy warnings
- policy-specific risk flags

## Important truth boundary
V2.4 still does **not** authorize live calibrated probabilities.

The correct interpretation is:

> “This live candidate resembles a historically validated elite replay policy.”

Not:

> “This live candidate has X% probability of success.”

## New environment variables

These are included in `render.yaml`:

```text
LIVE_POLICY_MIN_REPLAY_SURFACE_SCORE=80
LIVE_POLICY_MAX_POLICY_STOP_RATE=0.55
LIVE_POLICY_HIGH_COMPOSITE_RANK_WARNING=5
LIVE_POLICY_REQUIRE_MODERATE_DATA_QUALITY=true
```

## Post-deploy test steps

1. Redeploy the full ZIP.
2. Confirm the header shows:
   - `v2.4.0`
   - `v2.4.0-live-elite-policy-overlay`
3. Confirm a completed V2.3/V2.4 replay exists. If not, run Replay / Calibration first.
4. Run one live scan.
5. Open Latest Results.
6. Confirm the table shows replay surface and policy overlay fields.
7. Download the scan pack.
8. Confirm the pack contains:
   - `live_policy_overlay_report.json`
   - `policy_eligible_candidates.csv`
   - `artifact_manifest.json`
   - `ranked_candidates.csv`
   - `ranked_candidates.json`

## What to upload back

- latest `*_scan_pack.zip`
- `live_policy_overlay_report.json`
- `policy_eligible_candidates.csv`
- `score_diagnostics.json`
- `coverage_diagnostics.json`
- `health.json`
- `status.json`

## Acceptance gate

V2.4 is successful if:

- scan completes cleanly
- scan-pack integrity passes
- live policy overlay report identifies the replay policy source
- live candidates show replay-surface score/rank
- `policy_eligible_candidates.csv` contains only candidates that pass risk-adjusted validated policy gates
- high-composite names without policy support are explicitly warned
