from __future__ import annotations

import json
import math
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sanitize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def sanitize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _sanitize_value(value) for key, value in row.items()}


def write_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, default=str))


def write_text(path: str | Path, text: str) -> None:
    Path(path).write_text(text)


def write_csv(path: str | Path, rows: List[Dict[str, Any]]) -> None:
    sanitized_rows = [sanitize_row(row) for row in rows]
    df = pd.DataFrame(sanitized_rows)
    df = df.where(pd.notnull(df), None)
    df.to_csv(path, index=False)


def zip_directory(directory: str | Path, zip_path: str | Path) -> Path:
    directory = Path(directory)
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(directory.rglob("*")):
            if file_path.is_file() and file_path != zip_path:
                zf.write(file_path, arcname=file_path.relative_to(directory))
    return zip_path
