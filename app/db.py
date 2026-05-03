from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from .config import load_settings


def get_connection() -> sqlite3.Connection:
    settings = load_settings()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT,
                ended_at TEXT,
                status TEXT,
                progress_current INTEGER,
                progress_total INTEGER,
                phase TEXT,
                message TEXT,
                universe_name TEXT,
                universe_size INTEGER,
                enrichment_size INTEGER,
                shortlist_size INTEGER,
                provider TEXT,
                settings_json TEXT,
                warnings_json TEXT,
                artifacts_dir TEXT,
                artifact_zip_path TEXT,
                summary_json TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                run_id TEXT,
                rank INTEGER,
                ticker TEXT,
                company_name TEXT,
                sector TEXT,
                industry TEXT,
                cik TEXT,
                overall_score REAL,
                structural_score REAL,
                catalyst_score REAL,
                timing_score REAL,
                confidence_band TEXT,
                reason_codes_json TEXT,
                risk_flags_json TEXT,
                latest_news_json TEXT,
                technical_summary TEXT,
                fundamental_summary TEXT,
                feature_snapshot_json TEXT,
                PRIMARY KEY (run_id, ticker)
            )
            """
        )
        existing_candidate_cols = {row[1] for row in cur.execute("PRAGMA table_info(candidates)").fetchall()}
        if "cik" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN cik TEXT")


def upsert_run(record: Dict[str, Any]) -> None:
    columns = list(record.keys())
    placeholders = ", ".join(["?"] * len(columns))
    update_clause = ", ".join([f"{col}=excluded.{col}" for col in columns if col != "run_id"])
    sql = f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(run_id) DO UPDATE SET {update_clause}"
    with db_cursor() as cur:
        cur.execute(sql, [record[col] for col in columns])


def replace_candidates(run_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
        for row in rows:
            columns = list(row.keys())
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT INTO candidates ({', '.join(columns)}) VALUES ({placeholders})"
            cur.execute(sql, [row[col] for col in columns])


def get_latest_run() -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        row = cur.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        row = cur.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        rows = cur.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_candidates(run_id: str) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM candidates WHERE run_id = ? ORDER BY rank ASC, overall_score DESC",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_candidate(run_id: str, ticker: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        row = cur.execute(
            "SELECT * FROM candidates WHERE run_id = ? AND ticker = ?",
            (run_id, ticker.upper()),
        ).fetchone()
    return dict(row) if row else None


def deserialize_run(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not run:
        return None
    for key in ["settings_json", "warnings_json", "summary_json"]:
        value = run.get(key)
        if isinstance(value, str) and value:
            try:
                run[key[:-5] if key.endswith('_json') else key] = json.loads(value)
            except json.JSONDecodeError:
                run[key[:-5] if key.endswith('_json') else key] = value
    return run


def deserialize_candidate(candidate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidate:
        return None
    for key in ["reason_codes_json", "risk_flags_json", "latest_news_json", "feature_snapshot_json"]:
        value = candidate.get(key)
        base = key[:-5]
        if isinstance(value, str) and value:
            try:
                candidate[base] = json.loads(value)
            except json.JSONDecodeError:
                candidate[base] = value
    return candidate
