from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class AppSettings:
    app_name: str = os.getenv("APP_NAME", "news-fundamentals-technicals-stock-scanner")
    app_env: str = os.getenv("APP_ENV", "production")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    data_dir: str = os.getenv("DATA_DIR", "/var/data")
    database_path: str = os.getenv("DATABASE_PATH", "/var/data/scanner.db")
    artifacts_dir: str = os.getenv("ARTIFACTS_DIR", "/var/data/artifacts")
    settings_path: str = os.getenv("SETTINGS_PATH", "/var/data/settings.json")
    runtime_status_path: str = os.getenv("RUNTIME_STATUS_PATH", "/var/data/runtime_status.json")
    universe_cache_path: str = os.getenv("UNIVERSE_CACHE_PATH", "/var/data/universe_cache.json")

    default_provider: str = os.getenv("DATA_PROVIDER", "yfinance")
    demo_mode: bool = _env_bool("DEMO_MODE", False)
    default_universe_name: str = os.getenv("DEFAULT_UNIVERSE_NAME", "S&P 500")
    scan_ticker_limit: int = int(os.getenv("SCAN_TICKER_LIMIT", "500"))
    enrichment_limit: int = int(os.getenv("ENRICHMENT_LIMIT", "120"))
    shortlist_size: int = int(os.getenv("SHORTLIST_SIZE", "20"))
    lookback_days: int = int(os.getenv("LOOKBACK_DAYS", "320"))
    news_lookback_days: int = int(os.getenv("NEWS_LOOKBACK_DAYS", "7"))
    max_workers: int = int(os.getenv("MAX_WORKERS", "8"))
    scan_cooldown_seconds: int = int(os.getenv("SCAN_COOLDOWN_SECONDS", "5"))

    structural_weight: float = float(os.getenv("STRUCTURAL_WEIGHT", "0.35"))
    catalyst_weight: float = float(os.getenv("CATALYST_WEIGHT", "0.30"))
    timing_weight: float = float(os.getenv("TIMING_WEIGHT", "0.35"))

    yfinance_timeout_seconds: int = int(os.getenv("YFINANCE_TIMEOUT_SECONDS", "20"))
    requests_timeout_seconds: int = int(os.getenv("REQUESTS_TIMEOUT_SECONDS", "20"))
    yfinance_bulk_chunk_size: int = int(os.getenv("YFINANCE_BULK_CHUNK_SIZE", "100"))

    wikipedia_universe_url: str = os.getenv(
        "WIKIPEDIA_UNIVERSE_URL",
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    )

    finnhub_api_key: str = os.getenv("FINNHUB_API_KEY", "")
    polygon_api_key: str = os.getenv("POLYGON_API_KEY", "")
    alpaca_api_key: str = os.getenv("ALPACA_API_KEY", "")
    alpaca_api_secret: str = os.getenv("ALPACA_API_SECRET", "")
    alpaca_base_url: str = os.getenv("ALPACA_BASE_URL", "https://data.alpaca.markets")

    extra: Dict[str, Any] = field(default_factory=dict)

    def ensure_paths(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.artifacts_dir).mkdir(parents=True, exist_ok=True)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.settings_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.runtime_status_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.universe_cache_path).parent.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_safe_dict(self) -> Dict[str, Any]:
        return redact_sensitive_settings(asdict(self))




SENSITIVE_SETTING_KEYS = {
    "finnhub_api_key",
    "polygon_api_key",
    "alpaca_api_key",
    "alpaca_api_secret",
}


def redact_sensitive_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for key, value in payload.items():
        if key in SENSITIVE_SETTING_KEYS and value:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted

DEFAULTS = AppSettings()


MUTABLE_SETTING_KEYS = {
    "default_provider",
    "demo_mode",
    "scan_ticker_limit",
    "enrichment_limit",
    "shortlist_size",
    "lookback_days",
    "news_lookback_days",
    "max_workers",
    "structural_weight",
    "catalyst_weight",
    "timing_weight",
}


def _coerce_setting_value(key: str, value: Any) -> Any:
    bool_keys = {"demo_mode"}
    int_keys = {
        "scan_ticker_limit",
        "enrichment_limit",
        "shortlist_size",
        "lookback_days",
        "news_lookback_days",
        "max_workers",
    }
    float_keys = {"structural_weight", "catalyst_weight", "timing_weight"}
    if key in bool_keys:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if key in int_keys:
        return int(value)
    if key in float_keys:
        return float(value)
    return value




def _apply_v12_recommended_defaults(settings: AppSettings, raw_data: Dict[str, Any]) -> None:
    updated_from_ui = bool(raw_data.get("updated_from_ui"))
    if updated_from_ui:
        return
    if settings.default_universe_name == "S&P 500":
        if settings.scan_ticker_limit == 120:
            settings.scan_ticker_limit = 500
        if settings.enrichment_limit == 60:
            settings.enrichment_limit = 120


def load_settings() -> AppSettings:
    settings = AppSettings()
    settings.ensure_paths()
    path = Path(settings.settings_path)
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
        for key, value in data.items():
            if hasattr(settings, key):
                setattr(settings, key, _coerce_setting_value(key, value))
            else:
                settings.extra[key] = value
    _apply_v12_recommended_defaults(settings, data)
    return settings


def persist_settings(updated_values: Dict[str, Any]) -> AppSettings:
    settings = load_settings()
    for key, value in updated_values.items():
        if key not in MUTABLE_SETTING_KEYS:
            continue
        setattr(settings, key, _coerce_setting_value(key, value))
    payload = {key: getattr(settings, key) for key in sorted(MUTABLE_SETTING_KEYS)}
    payload["updated_from_ui"] = True
    Path(settings.settings_path).write_text(json.dumps(payload, indent=2))
    return settings
