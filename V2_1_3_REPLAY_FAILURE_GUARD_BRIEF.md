# V2.1.3 replay failure guard

This hotfix makes replay failure states honest and non-misleading.

## What changed
- Replay validation-pack download now returns a clear failure message when the latest replay failed before producing artifacts.
- Replay page only shows the validation-pack button when the replay completed and the full artifact set exists.
- Failed replay states now show a validation-log download instead.
- Runs page replay download column now prefers validation log for failed replays.
- Build truth updated to v2.1.3 / v2.1.3-replay-failure-guard.

## What this does not do
- It does not fix the underlying replay failure itself. It makes the app tell the truth and point the operator to the right artifact.
