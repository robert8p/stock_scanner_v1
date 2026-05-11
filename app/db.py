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
                opportunity_type TEXT,
                catalyst_truth_label TEXT,
                catalyst_support_level TEXT,
                catalyst_high_credibility_count INTEGER,
                catalyst_low_signal_ratio REAL,
                evidence_completeness_pct REAL,
                data_quality_label TEXT,
                price_last_timestamp TEXT,
                news_last_timestamp TEXT,
                PRIMARY KEY (run_id, ticker)
            )
            """
        )
        existing_candidate_cols = {row[1] for row in cur.execute("PRAGMA table_info(candidates)").fetchall()}
        if "cik" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN cik TEXT")
        if "opportunity_type" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN opportunity_type TEXT")
        if "catalyst_truth_label" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN catalyst_truth_label TEXT")
        if "catalyst_support_level" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN catalyst_support_level TEXT")
        if "catalyst_high_credibility_count" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN catalyst_high_credibility_count INTEGER")
        if "catalyst_low_signal_ratio" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN catalyst_low_signal_ratio REAL")
        if "evidence_completeness_pct" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN evidence_completeness_pct REAL")
        if "data_quality_label" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN data_quality_label TEXT")
        if "price_last_timestamp" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN price_last_timestamp TEXT")
        if "news_last_timestamp" not in existing_candidate_cols:
            cur.execute("ALTER TABLE candidates ADD COLUMN news_last_timestamp TEXT")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_runs (
                replay_id TEXT PRIMARY KEY,
                started_at TEXT,
                ended_at TEXT,
                status TEXT,
                progress_current INTEGER,
                progress_total INTEGER,
                phase TEXT,
                message TEXT,
                provider TEXT,
                replay_mode TEXT,
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
            CREATE TABLE IF NOT EXISTS shortlist_outcomes (
                run_id TEXT,
                ticker TEXT,
                company_name TEXT,
                entry_date TEXT,
                entry_price REAL,
                target_up_pct REAL,
                stop_down_pct REAL,
                horizon_days INTEGER,
                status TEXT,
                evaluated_at TEXT,
                days_elapsed INTEGER,
                max_return_pct REAL,
                min_return_pct REAL,
                end_return_pct REAL,
                hit_up_first INTEGER,
                hit_down_first INTEGER,
                outcome_note TEXT,
                updated_at TEXT,
                PRIMARY KEY (run_id, ticker)
            )
            """
        )


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


def upsert_shortlist_outcomes(rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with db_cursor() as cur:
        for row in rows:
            columns = list(row.keys())
            placeholders = ", ".join(["?"] * len(columns))
            update_clause = ", ".join([f"{col}=excluded.{col}" for col in columns if col not in {"run_id", "ticker"}])
            sql = f"INSERT INTO shortlist_outcomes ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(run_id, ticker) DO UPDATE SET {update_clause}"
            cur.execute(sql, [row[col] for col in columns])


def list_shortlist_outcomes(limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        if status:
            rows = cur.execute(
                "SELECT * FROM shortlist_outcomes WHERE status = ? ORDER BY entry_date DESC, ticker ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT * FROM shortlist_outcomes ORDER BY entry_date DESC, ticker ASC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def summarize_shortlist_outcomes() -> Dict[str, Any]:
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT status, COUNT(*) AS count FROM shortlist_outcomes GROUP BY status"
        ).fetchall()
    status_counts = {row["status"]: int(row["count"]) for row in rows}
    total = sum(status_counts.values())
    return {
        "total": total,
        "pending": status_counts.get("pending", 0),
        "target_hit": status_counts.get("target_hit", 0),
        "stop_hit": status_counts.get("stop_hit", 0),
        "expired": status_counts.get("expired", 0),
        "insufficient_data": status_counts.get("insufficient_data", 0),
    }


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



def upsert_replay_run(record: Dict[str, Any]) -> None:
    columns = list(record.keys())
    placeholders = ", ".join(["?"] * len(columns))
    update_clause = ", ".join([f"{col}=excluded.{col}" for col in columns if col != "replay_id"])
    sql = f"INSERT INTO replay_runs ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(replay_id) DO UPDATE SET {update_clause}"
    with db_cursor() as cur:
        cur.execute(sql, [record[col] for col in columns])


def get_latest_replay_run() -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        row = cur.execute("SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_replay_run(replay_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        row = cur.execute("SELECT * FROM replay_runs WHERE replay_id = ?", (replay_id,)).fetchone()
    return dict(row) if row else None


def list_replay_runs(limit: int = 50) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        rows = cur.execute("SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def deserialize_replay_run(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
