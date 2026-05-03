from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import md5
from typing import Dict, List

import numpy as np
import pandas as pd

from .base import BaseDataProvider, NewsItem, TickerDataBundle


class DemoProvider(BaseDataProvider):
    provider_name = "demo"

    POSITIVE_HEADLINES = [
        "raises outlook after strong demand",
        "announces strategic partnership expansion",
        "beats expectations on revenue and margins",
        "launches new product with enterprise traction",
        "wins multi-year contract with blue-chip customer",
    ]
    NEGATIVE_HEADLINES = [
        "faces lawsuit over product disclosure",
        "cuts guidance amid weaker demand",
        "downgraded after margin concerns",
        "investigation announced by regulator",
        "misses expectations on earnings",
    ]

    def _seed(self, ticker: str) -> int:
        return int(md5(ticker.encode()).hexdigest()[:8], 16)

    def fetch_bulk_price_history(self, tickers: List[str], lookback_days: int) -> Dict[str, pd.DataFrame]:
        return {ticker: self._make_price_history(ticker, lookback_days) for ticker in tickers}

    def _make_price_history(self, ticker: str, lookback_days: int) -> pd.DataFrame:
        rng = np.random.default_rng(self._seed(ticker))
        dates = pd.bdate_range(end=pd.Timestamp.utcnow().normalize(), periods=min(lookback_days, 320))
        drift = rng.uniform(-0.0003, 0.0012)
        volatility = rng.uniform(0.01, 0.03)
        start_price = rng.uniform(40, 320)
        returns = rng.normal(loc=drift, scale=volatility, size=len(dates))
        close = start_price * np.cumprod(1 + returns)
        open_ = close * (1 + rng.normal(0, 0.002, size=len(dates)))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0.003, 0.004, size=len(dates))))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0.003, 0.004, size=len(dates))))
        volume = rng.integers(1_000_000, 40_000_000, size=len(dates))
        return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=dates)

    def fetch_ticker_bundle(self, ticker: str, lookback_days: int, news_lookback_days: int) -> TickerDataBundle:
        seed = self._seed(ticker)
        rng = np.random.default_rng(seed)
        history = self._make_price_history(ticker, lookback_days)
        positive = seed % 5 != 0
        headline_pool = self.POSITIVE_HEADLINES if positive else self.NEGATIVE_HEADLINES
        news = []
        for idx in range(3):
            published = datetime.now(timezone.utc) - timedelta(days=idx)
            news.append(
                NewsItem(
                    title=f"{ticker} {headline_pool[idx % len(headline_pool)]}",
                    publisher="DemoWire",
                    link="https://example.com/demo-news",
                    published_at=published.isoformat(),
                    summary=f"Synthetic headline for {ticker} used when DEMO_MODE=true.",
                    related_tickers=[ticker],
                )
            )
        fundamentals = {
            "revenueGrowth": float(rng.uniform(-0.05, 0.35)),
            "earningsGrowth": float(rng.uniform(-0.1, 0.4)),
            "profitMargins": float(rng.uniform(0.02, 0.35)),
            "operatingMargins": float(rng.uniform(0.05, 0.4)),
            "debtToEquity": float(rng.uniform(5, 180)),
            "currentRatio": float(rng.uniform(0.8, 3.2)),
            "returnOnEquity": float(rng.uniform(0.02, 0.35)),
            "forwardPE": float(rng.uniform(10, 38)),
            "freeCashflowYield": float(rng.uniform(-0.01, 0.08)),
            "marketCap": float(rng.uniform(20e9, 800e9)),
        }
        return TickerDataBundle(
            ticker=ticker,
            company_name=f"{ticker} Holdings",
            sector="Information Technology" if seed % 2 == 0 else "Health Care",
            industry="Software" if seed % 2 == 0 else "Biotechnology",
            price_history=history,
            fundamentals=fundamentals,
            profile={"longName": f"{ticker} Holdings"},
            news=news,
        )
