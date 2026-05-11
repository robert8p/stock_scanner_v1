# V2.1 Replay Discrimination Hardening

## What this tranche does

This build hardens replay validation without pretending full live probability parity exists.

It adds:
- context-adjusted timing replay surface for historical validation
- benchmark regime classification (`risk_on`, `neutral`, `risk_off`)
- explicit discrimination gates
- score-band monotonicity diagnostics
- richer replay artifacts beyond the coarse V2.0 calibration pack

## New validation gates

The replay now evaluates whether the ranking surface is actually discriminative using:
- minimum observations
- minimum score-band size
- minimum Spearman score/outcome correlation
- minimum top-decile lift vs all observations
- minimum top-quintile lift vs all observations
- maximum allowed score-band monotonicity violations

These gates drive a new `discrimination_report.json`.

## New artifacts

Every validation pack now includes:
- `discrimination_report.json`
- `monotonicity_diagnostics.json`
- `quantile_lift_table.csv`
- `regime_slice_metrics.csv`

Existing replay artifacts remain.

## Important truth boundary

This build still does **not** authorize live calibrated probabilities for the full composite score.

Why:
- replay still lacks point-in-time historical fundamentals
- replay still lacks point-in-time historical catalyst/news inputs
- replay remains a timing-only boundary, now with stronger context-aware validation

## What I validated locally

- app compiles cleanly
- main pages render
- replay page renders
- replay runs complete in demo mode
- validation pack includes all required artifacts
- replay artifact integrity passes
- replay summary, discrimination report, monotonicity diagnostics, quantile table, and regime slices all load

## Redeploy notes

Use this ZIP as the full app replacement.

The build/version should show:
- app version: `v2.1.0`
- build id: `v2.1.0-replay-discrimination-hardening`

## What to test after deploy

1. Open the app and confirm the header shows `v2.1.0` and `v2.1.0-replay-discrimination-hardening`
2. Open **Replay / Calibration**
3. Run one replay
4. Download the validation pack
5. Confirm the pack contains:
   - `replay_summary.json`
   - `score_band_metrics.csv`
   - `calibration_table.csv`
   - `candidate_outcomes.csv`
   - `top_vs_rest_comparison.csv`
   - `quantile_lift_table.csv`
   - `regime_slice_metrics.csv`
   - `discrimination_report.json`
   - `monotonicity_diagnostics.json`
   - `replay_parity_assessment.json`
   - `replay_artifact_manifest.json`

## What to upload back

- latest `*_validation_pack.zip`
- `replay_summary.json`
- `discrimination_report.json`
- `monotonicity_diagnostics.json`
- `quantile_lift_table.csv`
- `regime_slice_metrics.csv`
- `replay_parity_assessment.json`
- `health.json`
- `status.json`

## Current answer on live probability display

**No.** This build improves replay discrimination diagnostics, but it still does not justify turning live calibrated probabilities on for the full composite score.
