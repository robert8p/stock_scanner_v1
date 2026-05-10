# V1.6.1 hotfix brief

## Root cause
A live scan could fail during forward outcome tracking with:

`Invalid comparison between dtype=datetime64[ns] and Timestamp`

The failure came from comparing a timezone-naive price-history index with a timezone-aware entry timestamp inside outcome evaluation.

## Fixes applied
1. Added timezone-safe forward-frame filtering so naive and aware timestamps are compared safely.
2. Made outcome evaluation fail-soft per row so one bad outcome row does not fail the full scan.
3. Ensured runtime status initializes the database before reading outcome tracking tables.
4. Preserved the V1.6 reliability controls already added.
5. Bumped deployed build truth to `v1.6.1` / `v1.6.1-outcome-timestamp-hotfix`.

## Validation performed
- Python compile check passed.
- Timezone-safe forward-frame filtering tested with both naive and UTC-aware pandas indexes.
- End-to-end demo scan completed successfully.
- Generated scan pack contains required artifacts including:
  - `artifact_manifest.json`
  - `shortlist_views.json`
  - `outcome_tracking_summary.json`

## Redeploy check
After redeploy, the UI should show:
- Build `v1.6.1`
- Build ID `v1.6.1-outcome-timestamp-hotfix`
