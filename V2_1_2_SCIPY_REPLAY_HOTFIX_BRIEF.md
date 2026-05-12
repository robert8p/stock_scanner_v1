# V2.1.2 replay SciPy hotfix

This hotfix fixes replay failures on Render caused by a missing optional SciPy dependency.

## What changed
- Added `scipy>=1.14,<2.0` to `requirements.txt`.
- Removed the replay engine's dependency on SciPy for Spearman correlation by computing Spearman as Pearson correlation on ranked series.
- Bumped build truth to `v2.1.2 / v2.1.2-scipy-replay-hotfix`.

## Why this matters
The replay page could load, but replay execution could still fail at runtime with `No module named 'scipy'` on hosts where SciPy was not installed. This hotfix fixes that failure path directly.
