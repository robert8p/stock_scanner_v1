from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

from .config import load_settings
from .storage import utc_now_iso


UNIVERSE_CACHE_TTL_DAYS = 7


LOCAL_FALLBACK_UNIVERSE = [
    {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology"},
    {"symbol": "MSFT", "name": "Microsoft Corp.", "sector": "Information Technology"},
    {"symbol": "NVDA", "name": "NVIDIA Corp.", "sector": "Information Technology"},
    {"symbol": "AMZN", "name": "Amazon.com Inc.", "sector": "Consumer Discretionary"},
    {"symbol": "META", "name": "Meta Platforms Inc.", "sector": "Communication Services"},
    {"symbol": "GOOGL", "name": "Alphabet Inc. Class A", "sector": "Communication Services"},
    {"symbol": "GOOG", "name": "Alphabet Inc. Class C", "sector": "Communication Services"},
    {"symbol": "TSLA", "name": "Tesla Inc.", "sector": "Consumer Discretionary"},
    {"symbol": "BRK.B", "name": "Berkshire Hathaway Class B", "sector": "Financials"},
    {"symbol": "JPM", "name": "JPMorgan Chase & Co.", "sector": "Financials"},
    {"symbol": "V", "name": "Visa Inc.", "sector": "Financials"},
    {"symbol": "MA", "name": "Mastercard Inc.", "sector": "Financials"},
    {"symbol": "LLY", "name": "Eli Lilly and Company", "sector": "Health Care"},
    {"symbol": "UNH", "name": "UnitedHealth Group Inc.", "sector": "Health Care"},
    {"symbol": "XOM", "name": "Exxon Mobil Corp.", "sector": "Energy"},
    {"symbol": "COST", "name": "Costco Wholesale Corp.", "sector": "Consumer Staples"},
    {"symbol": "WMT", "name": "Walmart Inc.", "sector": "Consumer Staples"},
    {"symbol": "HD", "name": "Home Depot Inc.", "sector": "Consumer Discretionary"},
    {"symbol": "PG", "name": "Procter & Gamble Co.", "sector": "Consumer Staples"},
    {"symbol": "JNJ", "name": "Johnson & Johnson", "sector": "Health Care"},
    {"symbol": "ABBV", "name": "AbbVie Inc.", "sector": "Health Care"},
    {"symbol": "AVGO", "name": "Broadcom Inc.", "sector": "Information Technology"},
    {"symbol": "CRM", "name": "Salesforce Inc.", "sector": "Information Technology"},
    {"symbol": "ORCL", "name": "Oracle Corp.", "sector": "Information Technology"},
    {"symbol": "AMD", "name": "Advanced Micro Devices Inc.", "sector": "Information Technology"},
    {"symbol": "ADBE", "name": "Adobe Inc.", "sector": "Information Technology"},
    {"symbol": "NFLX", "name": "Netflix Inc.", "sector": "Communication Services"},
    {"symbol": "KO", "name": "Coca-Cola Co.", "sector": "Consumer Staples"},
    {"symbol": "PEP", "name": "PepsiCo Inc.", "sector": "Consumer Staples"},
    {"symbol": "MRK", "name": "Merck & Co., Inc.", "sector": "Health Care"},
    {"symbol": "BAC", "name": "Bank of America Corp.", "sector": "Financials"},
    {"symbol": "MCD", "name": "McDonald's Corp.", "sector": "Consumer Discretionary"},
    {"symbol": "CVX", "name": "Chevron Corp.", "sector": "Energy"},
    {"symbol": "GE", "name": "GE Aerospace", "sector": "Industrials"},
    {"symbol": "CAT", "name": "Caterpillar Inc.", "sector": "Industrials"},
    {"symbol": "HON", "name": "Honeywell International Inc.", "sector": "Industrials"},
    {"symbol": "LIN", "name": "Linde plc", "sector": "Materials"},
    {"symbol": "LOW", "name": "Lowe's Companies, Inc.", "sector": "Consumer Discretionary"},
    {"symbol": "DIS", "name": "Walt Disney Co.", "sector": "Communication Services"},
    {"symbol": "INTC", "name": "Intel Corp.", "sector": "Information Technology"},
    {"symbol": "QCOM", "name": "QUALCOMM Incorporated", "sector": "Information Technology"},
    {"symbol": "TXN", "name": "Texas Instruments Incorporated", "sector": "Information Technology"},
    {"symbol": "AMGN", "name": "Amgen Inc.", "sector": "Health Care"},
    {"symbol": "BKNG", "name": "Booking Holdings Inc.", "sector": "Consumer Discretionary"},
    {"symbol": "SBUX", "name": "Starbucks Corp.", "sector": "Consumer Discretionary"},
    {"symbol": "GS", "name": "Goldman Sachs Group Inc.", "sector": "Financials"},
    {"symbol": "SPGI", "name": "S&P Global Inc.", "sector": "Financials"},
    {"symbol": "NOW", "name": "ServiceNow Inc.", "sector": "Information Technology"},
    {"symbol": "PLD", "name": "Prologis Inc.", "sector": "Real Estate"},
    {"symbol": "NEE", "name": "NextEra Energy Inc.", "sector": "Utilities"},
]


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
                # Legacy cache with no timestamp — treat as stale and try to refresh
                cache_fresh = False
            if cached_rows and cache_fresh:
                return cached_rows
        except json.JSONDecodeError:
            pass

    try:
        tables = pd.read_html(settings.wikipedia_universe_url)
        if tables:
            df = tables[0].rename(columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "sector"})
            df["symbol"] = df["symbol"].astype(str).str.replace(".", "-", regex=False)
            rows = df[["symbol", "name", "sector"]].to_dict(orient="records")
            cache_path.write_text(json.dumps({"fetched_at": utc_now_iso(), "rows": rows}, indent=2))
            return rows
    except Exception:
        pass

    # Refresh failed — prefer stale cached rows over the small hardcoded fallback
    if cached_rows:
        return cached_rows
    return LOCAL_FALLBACK_UNIVERSE
