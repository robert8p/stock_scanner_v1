from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from .providers.base import NewsItem, TickerDataBundle


SECTOR_ETF_MAP = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}

SECTOR_ALIASES = {
    "Technology": "Information Technology",
    "Healthcare": "Health Care",
    "Health Care": "Health Care",
    "Consumer Defensive": "Consumer Staples",
    "Consumer Cyclical": "Consumer Discretionary",
    "Financial Services": "Financials",
    "Basic Materials": "Materials",
}

POSITIVE_NEWS_WORDS = {
    "beat", "beats", "upgrade", "upgrades", "raised", "raises", "partnership", "contract",
    "approval", "launch", "launches", "growth", "strong", "record", "buyback", "accretive",
    "outperform", "expands", "expansion", "guidance", "surge", "demand",
}
NEGATIVE_NEWS_WORDS = {
    "miss", "misses", "cut", "cuts", "downgrade", "downgraded", "lawsuit", "investigation",
    "probe", "recall", "delay", "decline", "weak", "warning", "offering", "dilution", "drops",
}
HIGH_CREDIBILITY_PUBLISHERS = {"Reuters", "Bloomberg", "The Wall Street Journal", "Barrons", "MarketWatch", "CNBC"}
GENERIC_MARKET_PATTERNS = {
    "stocks to watch", "wall street", "stock market today", "market open", "market close",
    "futures rise", "futures fall", "s&p 500", "dow jones", "nasdaq", "magnificent 7",
    "top stocks", "best stocks", "stocks to buy", "investor attention", "what you should know",
    "here is what to know", "what to watch", "trending stock", "share price run", "surges on strong earnings",
    "solid growth stock", "good stock to buy", "momentum stock to buy", "too late to consider",
}
LOW_SIGNAL_PUBLISHERS = {"Zacks", "Simply Wall St.", "Insider Monkey", "GuruFocus.com", "Benzinga", "StockStory", "24/7 Wall St.", "InvestorsHub"}
LOW_SIGNAL_TITLE_PATTERNS = [
    r"why (?:is|did|does) .* stock",
    r"is .* a good stock to buy",
    r"is it too late to consider",
    r"here(?:'| i)?s why",
    r"solid growth stock",
    r"strong momentum stock",
    r"trending stock",
    r"what to know beyond why",
    r"what to watch",
]
COMPANY_STOPWORDS = {"inc", "corp", "corporation", "company", "co", "holdings", "group", "class", "plc", "ltd", "the"}



def normalize_sector_name(sector: str | None) -> str:
    raw = (sector or "").strip()
    return SECTOR_ALIASES.get(raw, raw)


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        val = float(value)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except Exception:
        return None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_timing_metrics(price_history: pd.DataFrame, spy_history: pd.DataFrame | None, sector_history: pd.DataFrame | None) -> Dict[str, Any]:
    if price_history is None or price_history.empty or "Close" not in price_history.columns:
        return {"warnings": ["Missing price history"]}

    df = price_history.copy().dropna(subset=["Close"])
    close = df["Close"]
    volume = df["Volume"] if "Volume" in df.columns else pd.Series(index=df.index, dtype=float)
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    rsi14 = compute_rsi(close, 14)
    avg_vol20 = volume.rolling(20).mean() if not volume.empty else pd.Series(index=df.index, dtype=float)
    latest_close = float(close.iloc[-1])
    latest_volume = float(volume.iloc[-1]) if not volume.empty else None
    latest_avg_vol20 = float(avg_vol20.iloc[-1]) if not avg_vol20.dropna().empty else None
    volume_ratio = (latest_volume / latest_avg_vol20) if latest_volume and latest_avg_vol20 else None
    one_month_return = latest_close / float(close.iloc[-21]) - 1 if len(close) > 21 else None
    three_month_return = latest_close / float(close.iloc[-63]) - 1 if len(close) > 63 else None
    six_month_return = latest_close / float(close.iloc[-126]) - 1 if len(close) > 126 else None
    dist_to_52w_high = latest_close / float(close.tail(252).max()) - 1 if len(close) >= 50 else None
    breakout = bool(latest_close >= float(close.tail(20).max()) and (volume_ratio or 0) >= 1.1)

    rs_spy = None
    if spy_history is not None and not spy_history.empty and len(spy_history) >= 63:
        spy_close = spy_history["Close"].dropna()
        if len(spy_close) > 63 and three_month_return is not None:
            rs_spy = three_month_return - (float(spy_close.iloc[-1]) / float(spy_close.iloc[-63]) - 1)

    rs_sector = None
    if sector_history is not None and not sector_history.empty and len(sector_history) >= 63:
        sector_close = sector_history["Close"].dropna()
        if len(sector_close) > 63 and three_month_return is not None:
            rs_sector = three_month_return - (float(sector_close.iloc[-1]) / float(sector_close.iloc[-63]) - 1)

    return {
        "latest_close": latest_close,
        "sma20": _safe_float(sma20.iloc[-1]),
        "sma50": _safe_float(sma50.iloc[-1]),
        "sma200": _safe_float(sma200.iloc[-1]),
        "rsi14": _safe_float(rsi14.iloc[-1]),
        "volume_ratio": _safe_float(volume_ratio),
        "one_month_return": _safe_float(one_month_return),
        "three_month_return": _safe_float(three_month_return),
        "six_month_return": _safe_float(six_month_return),
        "dist_to_52w_high": _safe_float(dist_to_52w_high),
        "relative_strength_vs_spy": _safe_float(rs_spy),
        "relative_strength_vs_sector": _safe_float(rs_sector),
        "breakout": breakout,
        "warnings": [],
    }


def score_timing(metrics: Dict[str, Any]) -> Tuple[float, List[str], List[str], str]:
    warnings = list(metrics.get("warnings", []))
    reasons: List[str] = []
    risks: List[str] = []
    score = 30.0

    latest = metrics.get("latest_close")
    sma20 = metrics.get("sma20")
    sma50 = metrics.get("sma50")
    sma200 = metrics.get("sma200")
    if latest and sma20 and latest > sma20:
        score += 6
        reasons.append("Price above 20-day average")
    else:
        score -= 4
        risks.append("Price not above 20-day average")
    if latest and sma50 and latest > sma50:
        score += 7
        reasons.append("Price above 50-day average")
    else:
        score -= 5
        risks.append("Price not above 50-day average")
    if latest and sma200 and latest > sma200:
        score += 8
        reasons.append("Price above 200-day average")
    else:
        score -= 10
        risks.append("Price below 200-day average")

    one_month = metrics.get("one_month_return")
    three_month = metrics.get("three_month_return")
    if one_month is not None:
        score += clamp(one_month * 80, -8, 8)
        if one_month > 0.04:
            reasons.append("Positive 1-month momentum")
        elif one_month < 0:
            risks.append("Negative 1-month momentum")
    if three_month is not None:
        score += clamp(three_month * 60, -10, 10)
        if three_month > 0.08:
            reasons.append("Positive 3-month momentum")
        elif three_month < 0:
            risks.append("Negative 3-month momentum")

    rs_spy = metrics.get("relative_strength_vs_spy")
    if rs_spy is not None:
        score += clamp(rs_spy * 100, -8, 8)
        if rs_spy > 0.03:
            reasons.append("Outperforming SPY")
        elif rs_spy < 0:
            risks.append("Lagging SPY")

    rs_sector = metrics.get("relative_strength_vs_sector")
    if rs_sector is not None:
        score += clamp(rs_sector * 90, -7, 7)
        if rs_sector > 0.02:
            reasons.append("Outperforming sector ETF")
        elif rs_sector < 0:
            risks.append("Lagging sector ETF")
    else:
        risks.append("Sector-relative strength unavailable")

    rsi = metrics.get("rsi14")
    if rsi is not None:
        if 52 <= rsi <= 67:
            score += 7
            reasons.append("RSI in constructive trend range")
        elif 67 < rsi <= 74:
            score += 3
            reasons.append("RSI strong but not extreme")
        elif 74 < rsi <= 78:
            score -= 2
            risks.append("RSI approaching stretched territory")
        elif rsi > 78:
            score -= 8
            risks.append("RSI looks stretched")
        elif rsi < 40:
            score -= 6
            risks.append("RSI below healthy trend zone")

    volume_ratio = metrics.get("volume_ratio")
    if volume_ratio is not None:
        if volume_ratio >= 1.4:
            score += 5
            reasons.append("Volume running above 20-day average")
        elif volume_ratio >= 1.05:
            score += 2
            reasons.append("Volume at least modestly supportive")
        elif volume_ratio < 0.7:
            score -= 4
            risks.append("Volume muted versus 20-day average")

    dist_to_high = metrics.get("dist_to_52w_high")
    if dist_to_high is not None:
        if -0.15 <= dist_to_high <= -0.03:
            score += 4
            reasons.append("Trading close to 52-week highs without extreme stretch")
        elif -0.03 < dist_to_high <= -0.01:
            score += 1
        elif dist_to_high > -0.01:
            score -= 5
            risks.append("Very close to 52-week highs; could be extended")
        elif dist_to_high < -0.25:
            score -= 6
            risks.append("Far below 52-week highs")

    if metrics.get("breakout"):
        score += 4
        reasons.append("Potential breakout with volume support")

    summary_bits = []
    if latest:
        summary_bits.append(f"close {latest:.2f}")
    if one_month is not None and three_month is not None:
        summary_bits.append(f"1m {one_month:.1%}, 3m {three_month:.1%}")
    if rsi is not None:
        summary_bits.append(f"RSI {rsi:.1f}")
    if volume_ratio is not None:
        summary_bits.append(f"vol ratio {volume_ratio:.2f}x")
    technical_summary = "; ".join(summary_bits) if summary_bits else "Timing data incomplete"
    return round(clamp(score), 2), reasons[:5], warnings + risks[:5], technical_summary

def score_structural(bundle: TickerDataBundle) -> Tuple[float, List[str], List[str], str]:
    f = bundle.fundamentals or {}
    reasons: List[str] = []
    risks: List[str] = []
    score = 45.0

    revenue_growth = _safe_float(f.get("revenueGrowth"))
    if revenue_growth is not None:
        score += clamp(revenue_growth * 60, -8, 12)
        if revenue_growth > 0.08:
            reasons.append("Healthy revenue growth")
        elif revenue_growth < 0:
            risks.append("Revenue growth negative")
    else:
        risks.append("Revenue growth unavailable")

    earnings_growth = _safe_float(f.get("earningsGrowth"))
    if earnings_growth is not None:
        score += clamp(earnings_growth * 50, -8, 10)
        if earnings_growth > 0.10:
            reasons.append("Earnings growth supportive")
        elif earnings_growth < 0:
            risks.append("Earnings growth negative")

    profit_margins = _safe_float(f.get("profitMargins"))
    if profit_margins is not None:
        score += clamp(profit_margins * 80, -6, 8)
        if profit_margins > 0.15:
            reasons.append("Strong profit margins")
        elif profit_margins < 0.05:
            risks.append("Thin profit margins")

    operating_margins = _safe_float(f.get("operatingMargins"))
    if operating_margins is not None:
        score += clamp(operating_margins * 60, -5, 6)
        if operating_margins > 0.15:
            reasons.append("Operating margins healthy")

    debt_to_equity = _safe_float(f.get("debtToEquity"))
    if debt_to_equity is not None:
        if debt_to_equity < 0:
            score -= 6
            risks.append("Negative book equity (distressed balance sheet)")
        elif debt_to_equity < 60:
            score += 6
            reasons.append("Balance sheet looks manageable")
        elif debt_to_equity > 180:
            score -= 8
            risks.append("Debt to equity elevated")

    current_ratio = _safe_float(f.get("currentRatio"))
    if current_ratio is not None:
        if current_ratio >= 1.2:
            score += 4
            reasons.append("Current ratio healthy")
        elif current_ratio < 0.9:
            score -= 4
            risks.append("Current ratio weak")

    roe = _safe_float(f.get("returnOnEquity"))
    if roe is not None:
        if roe > 0.12:
            score += 5
            reasons.append("Return on equity supportive")
        elif roe < 0.05:
            score -= 3
            risks.append("Return on equity modest")

    fcf_yield = _safe_float(f.get("freeCashflowYield"))
    if fcf_yield is not None:
        score += clamp(fcf_yield * 120, -5, 7)
        if fcf_yield > 0.03:
            reasons.append("Free cash flow yield supportive")
        elif fcf_yield < 0:
            risks.append("Free cash flow yield negative")

    forward_pe = _safe_float(f.get("forwardPE"))
    if forward_pe is not None:
        if 10 <= forward_pe <= 28:
            score += 4
            reasons.append("Valuation not obviously stretched")
        elif forward_pe > 40:
            score -= 4
            risks.append("Forward PE looks demanding")

    summary_bits = []
    if revenue_growth is not None:
        summary_bits.append(f"rev growth {revenue_growth:.1%}")
    if profit_margins is not None:
        summary_bits.append(f"profit margin {profit_margins:.1%}")
    if debt_to_equity is not None:
        summary_bits.append(f"D/E {debt_to_equity:.0f}")
    if forward_pe is not None:
        summary_bits.append(f"fwd PE {forward_pe:.1f}")
    fundamental_summary = "; ".join(summary_bits) if summary_bits else "Fundamental data incomplete"
    return round(clamp(score), 2), reasons[:5], risks[:5], fundamental_summary


def _company_tokens(company_name: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z]{3,}", (company_name or "").lower())
    return [t for t in tokens if t not in COMPANY_STOPWORDS]


def _mentions_ticker(text: str, ticker: str) -> bool:
    return bool(ticker and re.search(rf"\b{re.escape(ticker.lower())}\b", text))


def _mentions_company(company_name: str, text: str) -> int:
    tokens = [token for token in _company_tokens(company_name) if len(token) >= 4]
    return sum(1 for token in tokens if token in text)


def _news_relevance(item: NewsItem, ticker: str, company_name: str) -> tuple[float, Dict[str, Any]]:
    title = (item.title or "").lower()
    summary = (item.summary or "").lower()
    text = f"{title} {summary}"
    related = {str(t).upper() for t in (item.related_tickers or []) if t}
    ticker_in_title_or_summary = _mentions_ticker(text, ticker)
    company_hits = _mentions_company(company_name, text)
    related_match = ticker.upper() in related
    generic_pattern = any(pattern in text for pattern in GENERIC_MARKET_PATTERNS)
    low_signal_title = any(re.search(pattern, title) for pattern in LOW_SIGNAL_TITLE_PATTERNS)
    multi_ticker_noise = len(related) >= 4

    relevance = 0.0
    if ticker_in_title_or_summary:
        relevance += 1.4
    if company_hits >= 2:
        relevance += 1.0
    elif company_hits == 1:
        relevance += 0.6
    if related_match:
        relevance += 0.35
    if generic_pattern:
        relevance -= 1.0
    if low_signal_title:
        relevance -= 0.8
    if multi_ticker_noise:
        relevance -= 0.5
    if not ticker_in_title_or_summary and company_hits == 0 and not related_match:
        relevance -= 1.2

    signals = {
        "ticker_in_text": ticker_in_title_or_summary,
        "company_hits": company_hits,
        "related_match": related_match,
        "generic_pattern": generic_pattern,
        "low_signal_title": low_signal_title,
        "multi_ticker_noise": multi_ticker_noise,
    }
    return relevance, signals

def classify_catalyst_truth(metrics: Dict[str, Any], catalyst_score: float | None = None) -> Dict[str, Any]:
    relevant_count = int(metrics.get("ticker_relevant_headline_count") or 0)
    positive_hits = int(metrics.get("positive_hits") or 0)
    negative_hits = int(metrics.get("negative_hits") or 0)
    high_credibility_count = int(metrics.get("high_credibility_relevant_count") or 0)
    unique_publishers = int(metrics.get("unique_relevant_publishers") or 0)
    low_signal_ratio = float(metrics.get("low_signal_relevant_ratio") or 0.0)

    has_positive_skew = positive_hits > negative_hits
    if high_credibility_count >= 1 and relevant_count >= 1 and has_positive_skew and low_signal_ratio < 0.5:
        support_level = "backed"
        opportunity_type = "Catalyst-backed opportunity"
        truth_label = "Catalyst-backed"
    elif relevant_count >= 2 and has_positive_skew and unique_publishers >= 2 and low_signal_ratio < 0.35:
        support_level = "supported"
        opportunity_type = "Catalyst-supported opportunity"
        truth_label = "Catalyst-supported"
    elif relevant_count >= 1 and positive_hits >= negative_hits and low_signal_ratio < 0.65:
        support_level = "mixed"
        opportunity_type = "Quality/momentum opportunity"
        truth_label = "Catalyst mixed / unconfirmed"
    else:
        support_level = "weak"
        opportunity_type = "Quality/momentum opportunity"
        truth_label = "Catalyst weak / unconfirmed"

    rank_penalty = {
        "backed": 0.0,
        "supported": 1.0,
        "mixed": 3.0,
        "weak": 6.0,
    }[support_level]
    if catalyst_score is not None and catalyst_score < 35:
        rank_penalty += 1.0

    return {
        "support_level": support_level,
        "opportunity_type": opportunity_type,
        "truth_label": truth_label,
        "rank_penalty": rank_penalty,
    }



def score_catalyst(news_items: List[NewsItem], ticker: str = "", company_name: str = "") -> Tuple[float, List[str], List[str], Dict[str, Any], List[Dict[str, Any]]]:
    empty_metrics = {
        "headline_count": 0,
        "ticker_relevant_headline_count": 0,
        "positive_hits": 0,
        "negative_hits": 0,
        "filtered_generic_count": 0,
        "filtered_irrelevant_count": 0,
        "high_credibility_relevant_count": 0,
        "low_signal_relevant_count": 0,
        "unique_relevant_publishers": 0,
        "low_signal_relevant_ratio": 0.0,
        "credible_positive_headline_count": 0,
        "support_level": "weak",
        "opportunity_type": "Quality/momentum opportunity",
        "truth_label": "Catalyst weak / unconfirmed",
        "rank_penalty": 6.0,
    }
    if not news_items:
        return 26.0, ["No recent catalyst confirmation"], ["Recent news sparse or unavailable"], empty_metrics, []

    now = datetime.now(timezone.utc)
    score = 28.0
    reasons: List[str] = []
    risks: List[str] = []
    positive_hits = 0
    negative_hits = 0
    filtered_generic_count = 0
    filtered_irrelevant_count = 0
    high_credibility_relevant_count = 0
    low_signal_relevant_count = 0
    credible_positive_headline_count = 0
    prepared_news: List[Dict[str, Any]] = []
    seen_titles = set()
    relevant_count = 0
    relevant_publishers: set[str] = set()

    for item in news_items[:15]:
        normalized_title = (item.title or "").strip().lower()
        if not normalized_title or normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)

        relevance, signals = _news_relevance(item, ticker, company_name)
        if signals["generic_pattern"] or signals["low_signal_title"]:
            filtered_generic_count += 1
        if relevance < 1.0 or ((signals["generic_pattern"] or signals["low_signal_title"]) and not signals["ticker_in_text"] and signals["company_hits"] == 0):
            filtered_irrelevant_count += 1
            continue

        text_blob = f"{item.title} {item.summary}".lower()
        pos_count = sum(1 for word in POSITIVE_NEWS_WORDS if word in text_blob)
        neg_count = sum(1 for word in NEGATIVE_NEWS_WORDS if word in text_blob)
        positive_hits += pos_count
        negative_hits += neg_count

        publisher = item.publisher or ""
        high_credibility = publisher in HIGH_CREDIBILITY_PUBLISHERS
        low_signal = publisher in LOW_SIGNAL_PUBLISHERS
        if high_credibility:
            credibility = 1.2
            high_credibility_relevant_count += 1
        elif low_signal:
            credibility = 0.15
            low_signal_relevant_count += 1
        else:
            credibility = 0.55

        recency_weight = 0.55
        if item.published_at:
            try:
                published_dt = datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
                if published_dt.tzinfo is None:
                    published_dt = published_dt.replace(tzinfo=timezone.utc)
                hours_old = max((now - published_dt).total_seconds() / 3600.0, 0.0)
                recency_weight = 1.0 if hours_old <= 24 else 0.75 if hours_old <= 72 else 0.45
            except Exception:
                pass

        sentiment_signal = (pos_count * 2.6) - (neg_count * 3.2)
        mention_bonus = 1.15 if signals["ticker_in_text"] or signals["company_hits"] >= 1 else 0.75
        delta = sentiment_signal * credibility * recency_weight * min(max(relevance, 1.0), 2.0) * mention_bonus

        if low_signal:
            if not signals["ticker_in_text"] and not signals["related_match"]:
                delta *= 0.2
            else:
                delta *= 0.4
        if signals["generic_pattern"]:
            delta *= 0.35
        if signals["low_signal_title"]:
            delta *= 0.25
        if relevance < 1.3:
            delta *= 0.7

        if high_credibility and pos_count > neg_count:
            credible_positive_headline_count += 1

        score += delta
        relevant_count += 1
        if publisher:
            relevant_publishers.add(publisher)
        prepared_news.append({
            "title": item.title,
            "publisher": publisher,
            "published_at": item.published_at,
            "link": item.link,
            "summary": item.summary,
            "sentiment_delta": round(delta, 2),
            "relevance": round(relevance, 2),
            "related_tickers": list(item.related_tickers or []),
            "high_credibility": high_credibility,
            "low_signal_publisher": low_signal,
        })

    if relevant_count == 0:
        metrics = {**empty_metrics, "headline_count": len(news_items), "filtered_generic_count": filtered_generic_count, "filtered_irrelevant_count": filtered_irrelevant_count}
        return 24.0, ["No ticker-specific recent catalyst confirmation"], ["News feed was generic or weakly related"], metrics, []

    low_signal_ratio = low_signal_relevant_count / relevant_count if relevant_count else 0.0

    if positive_hits > negative_hits:
        reasons.append("Ticker-specific recent news flow skewing positive")
    if positive_hits >= 3:
        reasons.append("Multiple positive catalyst terms in relevant headlines")
    if high_credibility_relevant_count >= 1:
        reasons.append("At least one higher-credibility relevant headline present")
    if credible_positive_headline_count >= 1:
        reasons.append("Higher-credibility catalyst headline skewed positive")
    if len(relevant_publishers) >= 3:
        reasons.append("Catalyst evidence spread across multiple publishers")

    if negative_hits > positive_hits:
        risks.append("Ticker-specific recent news flow skewing negative")
    if negative_hits >= 2:
        risks.append("Negative catalyst terms present in relevant headlines")
    if high_credibility_relevant_count == 0:
        score -= 8
        risks.append("No higher-credibility catalyst headline retained")
    if relevant_count < 2:
        score -= 4
        risks.append("Catalyst confirmation thin")
    if low_signal_ratio >= 0.6:
        score -= 12
        risks.append("Catalyst evidence dominated by low-signal publishers")
    elif low_signal_ratio >= 0.35:
        score -= 6
        risks.append("Catalyst evidence leans on low-signal publishers")

    if high_credibility_relevant_count == 0 and low_signal_ratio >= 0.35:
        score = min(score, 32.0)
    elif high_credibility_relevant_count == 0 and relevant_count < 2:
        score = min(score, 34.0)

    score = round(clamp(score), 2)
    metrics = {
        "headline_count": len(news_items),
        "ticker_relevant_headline_count": relevant_count,
        "positive_hits": positive_hits,
        "negative_hits": negative_hits,
        "filtered_generic_count": filtered_generic_count,
        "filtered_irrelevant_count": filtered_irrelevant_count,
        "high_credibility_relevant_count": high_credibility_relevant_count,
        "low_signal_relevant_count": low_signal_relevant_count,
        "unique_relevant_publishers": len(relevant_publishers),
        "low_signal_relevant_ratio": round(low_signal_ratio, 3),
        "credible_positive_headline_count": credible_positive_headline_count,
    }
    metrics.update(classify_catalyst_truth(metrics, score))
    prepared_news = sorted(prepared_news, key=lambda item: (item.get("high_credibility", False), not item.get("low_signal_publisher", False), item.get("relevance", 0), item.get("sentiment_delta", 0)), reverse=True)
    return score, reasons[:6], risks[:6], metrics, prepared_news[:5]


def confidence_band(score: float, catalyst_support_level: str = "mixed") -> str:
    if catalyst_support_level == "weak":
        if score >= 75:
            return "Quality/momentum watchlist"
        if score >= 60:
            return "Constructive watchlist"
        if score >= 50:
            return "Mixed / neutral"
        return "Weak / avoid for now"
    if catalyst_support_level == "mixed":
        if score >= 80:
            return "Quality/momentum leader"
        if score >= 70:
            return "Constructive watchlist"
        if score >= 60:
            return "Constructive watchlist"
        if score >= 50:
            return "Mixed / neutral"
        return "Weak / avoid for now"
    if score >= 80:
        return "Very strong setup"
    if score >= 70:
        return "Strong setup"
    if score >= 60:
        return "Constructive watchlist"
    if score >= 50:
        return "Mixed / neutral"
    return "Weak / avoid for now"

