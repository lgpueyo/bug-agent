"""Unit tests for bugagent.db"""
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta

from bugagent.db import (
    apply_migrations,
    insert_run,
    update_run,
    get_run,
    list_runs,
    add_claim,
    release_claim,
    get_stale_claims,
    init_db,
    get_connection,
)


def make_conn():
    """Create an in-memory SQLite connection with migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


# ---------------------------------------------------------------------------
# init_db / schema
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_path):
    db_file = str(tmp_path / "test.db")
    init_db(db_file)
    conn = get_connection(db_file)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "runs" in tables
    assert "claims" in tables
    assert "schema_version" in tables
    conn.close()


def test_schema_version_is_set():
    conn = make_conn()
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row[0] >= 1


def test_migrations_are_idempotent():
    conn = make_conn()
    # Applying again should not raise
    apply_migrations(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row[0] >= 1


# ---------------------------------------------------------------------------
# insert_run / get_run
# ---------------------------------------------------------------------------

def test_insert_run_returns_id():
    conn = make_conn()
    run_id = insert_run(conn, issue_number=42, issue_title="Test bug")
    assert isinstance(run_id, int)
    assert run_id >= 1


def test_insert_run_stores_correct_values():
    conn = make_conn()
    run_id = insert_run(conn, issue_number=7, issue_title="Another bug")
    row = get_run(conn, run_id)
    assert row is not None
    assert row["issue_number"] == 7
    assert row["issue_title"] == "Another bug"
    assert row["status"] is None
    assert row["finished_at"] is None
    assert row["alert_codes"] == "[]"


def test_get_run_returns_none_for_missing():
    conn = make_conn()
    assert get_run(conn, 99999) is None


# ---------------------------------------------------------------------------
# update_run
# ---------------------------------------------------------------------------

def test_update_run_changes_status():
    conn = make_conn()
    run_id = insert_run(conn, issue_number=1, issue_title="Bug")
    now = datetime.now(timezone.utc).isoformat()
    update_run(conn, run_id, status="pr_opened", finished_at=now)
    row = get_run(conn, run_id)
    assert row["status"] == "pr_opened"
    assert row["finished_at"] == now


def test_update_run_serializes_alert_codes_list():
    conn = make_conn()
    run_id = insert_run(conn, issue_number=2, issue_title="Bug2")
    update_run(conn, run_id, alert_codes=["TIMEOUT", "AGENT_FAILED"])
    row = get_run(conn, run_id)
    assert row["alert_codes"] == '["TIMEOUT", "AGENT_FAILED"]'


def test_update_run_noop_on_empty_kwargs():
    conn = make_conn()
    run_id = insert_run(conn, issue_number=3, issue_title="Bug3")
    # Should not raise
    update_run(conn, run_id)
    row = get_run(conn, run_id)
    assert row["status"] is None


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------

def test_list_runs_returns_all_by_default():
    conn = make_conn()
    insert_run(conn, 10, "A")
    insert_run(conn, 11, "B")
    runs = list_runs(conn)
    assert len(runs) == 2


def test_list_runs_filters_by_issue_number():
    conn = make_conn()
    insert_run(conn, 10, "A")
    insert_run(conn, 11, "B")
    runs = list_runs(conn, issue_number=10)
    assert len(runs) == 1
    assert runs[0]["issue_number"] == 10


def test_list_runs_filters_by_since():
    conn = make_conn()
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    insert_run(conn, 20, "Old")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    runs = list_runs(conn, since=future)
    assert len(runs) == 0
    runs_all = list_runs(conn, since=past)
    assert len(runs_all) == 1


# ---------------------------------------------------------------------------
# add_claim / release_claim
# ---------------------------------------------------------------------------

def test_add_claim_and_release_claim():
    conn = make_conn()
    run_id = insert_run(conn, 5, "Claimable")
    add_claim(conn, issue_number=5, run_id=run_id)
    row = conn.execute("SELECT * FROM claims WHERE issue_number=5").fetchone()
    assert row is not None
    assert row["run_id"] == run_id

    release_claim(conn, issue_number=5)
    row = conn.execute("SELECT * FROM claims WHERE issue_number=5").fetchone()
    assert row is None


def test_release_claim_is_idempotent():
    conn = make_conn()
    # Release on non-existent claim should not raise
    release_claim(conn, issue_number=999)


def test_add_claim_replace_on_duplicate():
    conn = make_conn()
    run_id1 = insert_run(conn, 6, "First")
    run_id2 = insert_run(conn, 6, "Second")
    add_claim(conn, issue_number=6, run_id=run_id1)
    add_claim(conn, issue_number=6, run_id=run_id2)
    rows = conn.execute("SELECT * FROM claims WHERE issue_number=6").fetchall()
    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id2


# ---------------------------------------------------------------------------
# get_stale_claims
# ---------------------------------------------------------------------------

def test_get_stale_claims_returns_expired_claims():
    conn = make_conn()
    run_id = insert_run(conn, 100, "Stale issue")
    # Manually set an old heartbeat
    old_heartbeat = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    update_run(conn, run_id, heartbeat_at=old_heartbeat)
    add_claim(conn, issue_number=100, run_id=run_id)

    stale = get_stale_claims(conn, threshold_minutes=30)
    assert len(stale) == 1
    assert stale[0]["issue_number"] == 100


def test_get_stale_claims_excludes_fresh_claims():
    conn = make_conn()
    run_id = insert_run(conn, 101, "Fresh issue")
    # heartbeat_at is set to now by insert_run
    add_claim(conn, issue_number=101, run_id=run_id)

    stale = get_stale_claims(conn, threshold_minutes=30)
    assert len(stale) == 0


def test_get_stale_claims_excludes_finished_runs():
    conn = make_conn()
    run_id = insert_run(conn, 102, "Finished issue")
    old_heartbeat = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    update_run(conn, run_id, heartbeat_at=old_heartbeat, finished_at=now, status="pr_opened")
    add_claim(conn, issue_number=102, run_id=run_id)

    stale = get_stale_claims(conn, threshold_minutes=30)
    assert len(stale) == 0
