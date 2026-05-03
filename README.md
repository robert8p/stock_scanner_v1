# News + Fundamentals + Technicals Stock Scanner — Version 1

This is a single-service FastAPI app for scanning a liquid US stock universe, ranking candidates by a transparent **Opportunity Score**, and packaging each run into a downloadable artifact bundle.

## What V1 does

- on-demand scan from the UI
- transparent component scores: structural, catalyst, timing
- ranked shortlist with explanations and caution flags
- candidate detail views
- per-run artifacts written to disk and downloadable as a scan pack ZIP
- visible runtime status and progress polling

## What V1 does not do

- calibrated probabilities
- historical replay / validation
- scheduled live monitoring / alerts

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000/scanner`.

## Helpful first test

If you want to verify deployment plumbing before hitting live data providers, set:

```bash
DEMO_MODE=true
```

Then run one scan. The app will generate deterministic synthetic data so the full UI and artifact flow can be tested end to end.
