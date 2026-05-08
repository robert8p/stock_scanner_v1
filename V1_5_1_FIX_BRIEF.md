# V1.5.1 Fix Brief — export/view parity hardening

This tranche fixes the exact gap found in live evidence from V1.5.

## Problem fixed

The UI had moved to grouped shortlist semantics, but the exported scan pack did not reliably prove the same semantics end to end.

In particular:
- `shortlist_views.json` was missing from the downloaded pack
- exported ranked files did not clearly carry both the score order and the default UI view order
- `/api/scan/latest` still defaulted to flat score ordering instead of the UI default view
- if the stored scan-pack zip was stale or incomplete, the download route returned the stale artifact instead of repairing it

## What changed

1. **Required artifact enforcement**
   - `shortlist_views.json` is treated as a required scan-pack artifact
   - `artifact_manifest.json` is added to every pack

2. **Export parity**
   - exported ranked files include both:
     - `score_rank`
     - `view_rank`
   - `view_rank` matches the default UI view (`catalyst_first`)
   - view-specific ranks remain available for other modes

3. **API/UI default alignment**
   - `/api/scan/latest` now defaults to `catalyst_first`, matching the Latest Results page

4. **Self-healing scan-pack download**
   - before returning the scan pack, the app checks whether required artifacts are actually present inside the zip
   - if the files exist on disk but the zip is stale/incomplete, it rebuilds the zip automatically and then serves it

## Operator impact

After this fix, the app should no longer have a mismatch where the UI shows grouped shortlist semantics but the downloaded pack looks like an older flat export.

## No new environment variables

This is a narrow parity/hardening tranche only.
