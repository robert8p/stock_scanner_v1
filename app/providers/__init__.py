from __future__ import annotations

from .demo import DemoProvider


def get_provider(name: str, demo_mode: bool, max_workers: int = 8):
    normalized = (name or "").strip().lower()
    if demo_mode or normalized == "demo":
        return DemoProvider()
    from .yfinance_provider import YFinanceProvider
    return YFinanceProvider(max_workers=max_workers)
