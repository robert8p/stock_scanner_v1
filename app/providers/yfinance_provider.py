from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd
import yfinance as yf

from .base import BaseDataProvider, NewsItem, TickerDataBundle


class YFinanceProvider(BaseDataProvider):
    provider_name = "yfinance"

    def __init__(self, max_workers: int = 8):
        self.max_workers = max_workers

    def fetch_bulk_price_history(self, tickers: List[str], lookback_days: int) -> Dict[str, pd.DataFrame]:
        if not tickers:
            return {}
        period_days = max(lookback_days, 260)
        try:
            data = yf.download(
                tickers=tickers,
                period=f"{period_days}d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception:
            return {}

        result: Dict[str, pd.DataFrame] = {}
        if isinstance(data.columns, pd.MultiIndex):
            for ticker in tickers:
                if ticker not in data.columns.get_level_values(0):
                    continue
                frame = data[ticker].dropna(how="all")
                if not frame.empty:
                    result[ticker] = frame
        else:
            frame = data.dropna(how="all")
            if not frame.empty and tickers:
                result[tickers[0]] = frame
        return result

    def fetch_ticker_bundle(self, ticker: str, lookback_days: int, news_lookback_days: int) -> TickerDataBundle:
        obj = yf.Ticker(ticker)
        warnings: List[str] = []
        history = None
        info = {}
        news_items: List[NewsItem] = []

        try:
            history = obj.history(period=f"{max(lookback_days, 260)}d", interval="1d", auto_adjust=False)
            if history is not None and history.empty:
                history = None
        except Exception as exc:
            warnings.append(f"Price history unavailable: {exc}")

        try:
            info = obj.get_info() or {}
        except Exception as exc:
            warnings.append(f"Fundamental profile unavailable: {exc}")
            info = {}

        try:
            raw_news = getattr(obj, "news", None)
            if raw_news is None and hasattr(obj, "get_news"):
                raw_news = obj.get_news()
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(news_lookback_days, 1))
            for item in raw_news or []:
                title = item.get("title") or item.get("content", {}).get("title") or ""
                publisher = item.get("publisher") or item.get("content", {}).get("provider", {}).get("displayName", "")
                link = item.get("link") or item.get("content", {}).get("canonicalUrl", {}).get("url", "")
                summary = item.get("summary") or item.get("content", {}).get("summary", "") or ""
                related_tickers = item.get("relatedTickers") or item.get("content", {}).get("finance", {}).get("stockTickers") or []
                if isinstance(related_tickers, str):
                    related_tickers = [related_tickers]
                published_at_raw = item.get("providerPublishTime") or item.get("content", {}).get("pubDate")
                published_at = ""
                if isinstance(published_at_raw, (int, float)):
                    published_dt = datetime.fromtimestamp(published_at_raw, timezone.utc)
                    if published_dt < cutoff:
                        continue
                    published_at = published_dt.isoformat()
                elif isinstance(published_at_raw, str):
                    try:
                        parsed = datetime.fromisoformat(published_at_raw.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        if parsed < cutoff:
                            continue
                        published_at = parsed.isoformat()
                    except Exception:
                        # Could not parse — keep raw string but apply no cutoff
                        published_at = published_at_raw
                news_items.append(
                    NewsItem(
                        title=title,
                        publisher=publisher,
                        link=link,
                        published_at=published_at,
                        summary=summary,
                        related_tickers=[str(t).upper() for t in related_tickers if t],
                    )
                )
        except Exception as exc:
            warnings.append(f"Recent news unavailable: {exc}")

        market_cap = info.get("marketCap")
        free_cash_flow = info.get("freeCashflow")
        free_cash_flow_yield = None
        if free_cash_flow and market_cap:
            try:
                free_cash_flow_yield = float(free_cash_flow) / float(market_cap)
            except Exception:
                free_cash_flow_yield = None

        fundamentals = {
            "revenueGrowth": info.get("revenueGrowth"),
            "earningsGrowth": info.get("earningsGrowth"),
            "profitMargins": info.get("profitMargins"),
            "operatingMargins": info.get("operatingMargins"),
            "debtToEquity": info.get("debtToEquity"),
            "currentRatio": info.get("currentRatio"),
            "returnOnEquity": info.get("returnOnEquity"),
            "forwardPE": info.get("forwardPE"),
            "enterpriseToEbitda": info.get("enterpriseToEbitda"),
            "freeCashflowYield": free_cash_flow_yield,
            "marketCap": market_cap,
        }

        return TickerDataBundle(
            ticker=ticker,
            company_name=info.get("longName") or info.get("shortName") or ticker,
            sector=info.get("sector") or "",
            industry=info.get("industry") or "",
            price_history=history,
            fundamentals=fundamentals,
            profile=info,
            news=news_items,
            warnings=warnings,
        )

    def fetch_many_ticker_bundles(self, tickers: List[str], lookback_days: int, news_lookback_days: int) -> Dict[str, TickerDataBundle]:
        result: Dict[str, TickerDataBundle] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self.fetch_ticker_bundle, ticker, lookback_days, news_lookback_days): ticker for ticker in tickers
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    result[ticker] = future.result()
                except Exception as exc:
                    result[ticker] = TickerDataBundle(ticker=ticker, warnings=[f"Bundle fetch failed: {exc}"])
        return result
