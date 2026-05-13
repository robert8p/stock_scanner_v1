# V2.2 Replay Feature-Surface Hardening

## Purpose
Improve replay discrimination without pretending full live-probability calibration exists.

## What changed
- Added replay-only continuation and rebound surface features.
- Blended raw timing score with replay surface scores instead of relying on raw timing plus small context adjustments alone.
- Added `surface_label` (`continuation`, `rebound`, `blended`) and surface component columns into replay candidate outputs.
- Added `surface_feature_report.json` to the validation pack.
- Updated replay surface identity to `feature_surface_hardened_v2_2`.
- Kept full-probability display disabled unless full-parity replay exists and the existing gates pass.

## New validation artifact
- `surface_feature_report.json`

## Intended effect
- Rescue valid rebound setups that were being scored near zero.
- Differentiate choppy middle-band names more clearly.
- Improve score/outcome monotonicity and correlation without claiming full parity.

## Truth boundary
This build still does **not** authorize live calibrated probabilities for the full composite score.
