# V2.1.1 Replay page hotfix

## Root cause
The Replay / Calibration page assumed the latest replay had V2.1 discrimination artifacts present. If the latest stored replay came from an older build or had partial artifacts, the Jinja template tried to iterate missing nested keys and the page returned Internal Server Error.

## Fixes
- make `latest_replay_payload()` always supply safe default structures for replay summary validation, discrimination report, and monotonicity diagnostics
- make the replay template tolerate missing/partial artifact payloads
- bump build truth to `v2.1.1` / `v2.1.1-replay-page-hotfix`

## Expected result
The Replay / Calibration page now opens cleanly even when the latest replay is from an older build or has incomplete discrimination artifacts.
