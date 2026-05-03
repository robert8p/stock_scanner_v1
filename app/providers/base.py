from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class NewsItem:
    title: str
    publisher: str = ""
    link: str = ""
    published_at: str = ""
    summary: str = ""
    related_tickers: List[str] = field(default_factory=list)


@dataclass
class TickerDataBundle:
    ticker: str
    company_name: str = ""
    sector: str = ""
    industry: str = ""
    price_history: Optional[pd.DataFrame] = None
    benchmark_history: Optional[pd.DataFrame] = None
    sector_benchmark_history: Optional[pd.DataFrame] = None
    fundamentals: Dict[str, float] = field(default_factory=dict)
    profile: Dict[str, str] = field(default_factory=dict)
    news: List[NewsItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class BaseDataProvider:
    provider_name = "base"

    def fetch_bulk_price_history(self, tickers: List[str], lookback_days: int) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError

    def fetch_ticker_bundle(self, ticker: str, lookback_days: int, news_lookback_days: int) -> TickerDataBundle:
        raise NotImplementedError
