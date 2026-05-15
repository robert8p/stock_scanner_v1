# V2.3 Elite-Policy Validation

## Purpose
V2.2 showed that the full ranking curve was still not clean enough for calibrated probabilities, but the elite end of the replay distribution looked meaningfully better. V2.3 therefore validates explicit elite-use policies instead of trying to force probability display.

## What changed
- Added an elite policy leaderboard to replay validation.
- Added explicit policy gates for:
  - minimum observations
  - minimum represented snapshots
  - minimum lift versus all observations
  - minimum average end return
  - maximum stop-hit rate
- Added new validation artifacts:
  - `elite_policy_leaderboard.csv`
  - `elite_policy_report.json`
- Added V2.3 replay summary fields:
  - `elite_policy_validation_status`
  - `elite_policy_recommended_count`
  - `elite_policy_watchlist_count`
  - `best_elite_policy_id`
  - `best_elite_policy_label`
- Updated the Replay / Calibration page with an elite policy leaderboard section and download buttons.

## Policies tested
- Top 3 per snapshot
- Top 5 per snapshot
- Top 10 per snapshot
- Score >= 85
- Score >= 90
- Score >= 80 in risk-on regime
- Score >= 80 in risk-off regime
- Score >= 80 + continuation surface
- Score >= 85 + continuation surface
- Score >= 90 + continuation surface
- Score >= 80 + rebound surface
- Score >= 85 + rebound surface
- Score >= 80 + risk-on continuation
- Score >= 80 + risk-off rebound

## Important truth boundary
This build does not enable calibrated live probabilities.

The output is a historical policy gate, not a probability table. A passing policy means: "this elite slice has historically cleared the validation thresholds in timing-only replay." It does not mean: "this candidate has X% probability of success."

## Required post-deploy test
1. Confirm the header shows `v2.3.0` and `v2.3.0-elite-policy-validation`.
2. Open Replay / Calibration.
3. Run one replay.
4. Download the validation pack.
5. Confirm the pack contains:
   - `elite_policy_leaderboard.csv`
   - `elite_policy_report.json`
   - `surface_feature_report.json`
   - `discrimination_report.json`
   - `monotonicity_diagnostics.json`
   - `replay_summary.json`
   - `replay_artifact_manifest.json`

## What to upload back
- latest `*_validation_pack.zip`
- `elite_policy_leaderboard.csv`
- `elite_policy_report.json`
- `replay_summary.json`
- `discrimination_report.json`
- `health.json`
- `status.json`
