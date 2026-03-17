import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 3

MIGRATIONS = [
    # migration 1
    """
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
    INSERT INTO schema_version VALUES (1);
    CREATE TABLE IF NOT EXISTS runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_number    INTEGER NOT NULL,
        issue_title     TEXT    NOT NULL,
        started_at      TEXT    NOT NULL,
        finished_at     TEXT,
        heartbeat_at    TEXT,
        status          TEXT,
        alert_codes     TEXT    NOT NULL DEFAULT '[]',
        pr_number       INTEGER,
        error_msg       TEXT,
        agent_log_path  TEXT
    );
    CREATE TABLE IF NOT EXISTS claims (
        issue_number    INTEGER PRIMARY KEY,
        claimed_at      TEXT    NOT NULL,
        run_id          INTEGER REFERENCES runs(id)
    );
    CREATE INDEX IF NOT EXISTS idx_runs_issue   ON runs(issue_number);
    CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
    CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);
    """,
    # migration 2 — placeholder
    "",
    # migration 3 — placeholder
    "",
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    apply_migrations(conn)
    conn.close()


def apply_migrations(conn: sqlite3.Connection) -> None:
    # Check current version
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] if row[0] is not None else 0
    except sqlite3.OperationalError:
        current = 0

    for i, sql in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        if sql.strip():
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
        if i > 1:  # version 1 inserts itself
            conn.execute("UPDATE schema_version SET version = ?", (i,))
        conn.commit()


def insert_run(conn: sqlite3.Connection, issue_number: int, issue_title: str) -> int:
    now = _now_utc()
    cur = conn.execute(
        "INSERT INTO runs (issue_number, issue_title, started_at, heartbeat_at, alert_codes) VALUES (?, ?, ?, ?, '[]')",
        (issue_number, issue_title, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_run(conn: sqlite3.Connection, run_id: int, **kwargs) -> None:
    if not kwargs:
        return
    if "alert_codes" in kwargs and isinstance(kwargs["alert_codes"], list):
        kwargs["alert_codes"] = json.dumps(kwargs["alert_codes"])
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [run_id]
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", vals)
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection, issue_number: int = None, since: str = None, limit: int = 100) -> list:
    q = "SELECT * FROM runs WHERE 1=1"
    params = []
    if issue_number is not None:
        q += " AND issue_number = ?"
        params.append(issue_number)
    if since is not None:
        q += " AND started_at >= ?"
        params.append(since)
    q += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def add_claim(conn: sqlite3.Connection, issue_number: int, run_id: int) -> None:
    now = _now_utc()
    conn.execute(
        "INSERT OR REPLACE INTO claims (issue_number, claimed_at, run_id) VALUES (?, ?, ?)",
        (issue_number, now, run_id),
    )
    conn.commit()


def release_claim(conn: sqlite3.Connection, issue_number: int) -> None:
    conn.execute("DELETE FROM claims WHERE issue_number = ?", (issue_number,))
    conn.commit()


def get_stale_claims(conn: sqlite3.Connection, threshold_minutes: int) -> list:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)).isoformat()
    rows = conn.execute(
        "SELECT c.issue_number, c.claimed_at, c.run_id FROM claims c "
        "LEFT JOIN runs r ON c.run_id = r.id "
        "WHERE (r.heartbeat_at IS NULL OR r.heartbeat_at < ?) AND r.finished_at IS NULL",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]
