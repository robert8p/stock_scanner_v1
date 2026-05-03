from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd

from .config import load_settings
from .storage import utc_now_iso


UNIVERSE_CACHE_TTL_DAYS = 7
LOCAL_BUNDLED_UNIVERSE_PATH = Path(__file__).resolve().parent / "data" / "sp500_constituents.csv"


def _shape_rows(df: pd.DataFrame) -> List[Dict[str, str]]:
    rename_map = {
        "Symbol": "symbol",
        "Security": "name",
        "GICS Sector": "sector",
        "GICS Sub-Industry": "industry",
        "CIK": "cik",
        "Date added": "date_added",
        "Founded": "founded",
        "Headquarters Location": "headquarters",
    }
    frame = df.rename(columns=rename_map).copy()
    for column in ["symbol", "name", "sector", "industry", "cik", "date_added", "founded", "headquarters"]:
        if column not in frame.columns:
            frame[column] = ""
    frame["symbol"] = frame["symbol"].astype(str).str.replace(".", "-", regex=False).str.upper()
    return frame[["symbol", "name", "sector", "industry", "cik", "date_added", "founded", "headquarters"]].fillna("").to_dict(orient="records")


def load_universe() -> List[Dict[str, str]]:
    settings = load_settings()
    cache_path = Path(settings.universe_cache_path)
    cached_rows: List[Dict[str, str]] = []
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text())
            cached_rows = payload.get("rows", []) or []
            fetched_at_raw = payload.get("fetched_at", "")
            cache_fresh = True
            if fetched_at_raw:
                try:
                    fetched_at = datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00"))
                    if fetched_at.tzinfo is None:
                        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                    cache_fresh = (datetime.now(timezone.utc) - fetched_at) < timedelta(days=UNIVERSE_CACHE_TTL_DAYS)
                except Exception:
                    cache_fresh = False
            else:
                cache_fresh = False
            if cached_rows and cache_fresh:
                return cached_rows
        except json.JSONDecodeError:
            pass

    try:
        tables = pd.read_html(settings.wikipedia_universe_url)
        if tables:
            rows = _shape_rows(tables[0])
            cache_path.write_text(json.dumps({"fetched_at": utc_now_iso(), "rows": rows, "source": "wikipedia"}, indent=2))
            return rows
    except Exception:
        pass

    if LOCAL_BUNDLED_UNIVERSE_PATH.exists():
        try:
            rows = _shape_rows(pd.read_csv(LOCAL_BUNDLED_UNIVERSE_PATH))
            cache_path.write_text(json.dumps({"fetched_at": utc_now_iso(), "rows": rows, "source": "bundled_csv"}, indent=2))
            return rows
        except Exception:
            pass

    if cached_rows:
        return cached_rows
    return []
