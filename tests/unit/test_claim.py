"""Unit tests for bugagent.claim"""
import pytest
from unittest.mock import MagicMock, patch, call

from bugagent.claim import claim_issue, release_claim, ClaimConflict
from bugagent.db import apply_migrations, insert_run
import sqlite3


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def make_label(name: str) -> MagicMock:
    label = MagicMock()
    label.name = name
    return label


def make_issue(number: int = 1, label_names: list = None) -> MagicMock:
    issue = MagicMock()
    issue.number = number
    issue.title = f"Test issue {number}"
    issue.labels = [make_label(n) for n in (label_names or [])]
    return issue


# ---------------------------------------------------------------------------
# test_claim_succeeds_on_unlabelled_issue
# ---------------------------------------------------------------------------

def test_claim_succeeds_on_unlabelled_issue():
    conn = make_conn()
    run_id = insert_run(conn, 1, "Test bug")
    issue = make_issue(number=1, label_names=["agent-queue"])
    gh_client = MagicMock()

    claim_issue(issue, gh_client, conn, "test-bot", run_id)

    issue.remove_from_labels.assert_called_once_with("agent-queue")
    issue.add_to_labels.assert_called_once_with("in-progress")
    issue.add_to_assignees.assert_called_once_with("test-bot")

    # Check claim was added to DB
    row = conn.execute("SELECT * FROM claims WHERE issue_number=1").fetchone()
    assert row is not None
    assert row["run_id"] == run_id


# ---------------------------------------------------------------------------
# test_claim_raises_conflict_on_in_progress_issue
# ---------------------------------------------------------------------------

def test_claim_raises_conflict_on_in_progress_issue():
    conn = make_conn()
    run_id = insert_run(conn, 2, "Already claimed")
    issue = make_issue(number=2, label_names=["agent-queue", "in-progress"])
    gh_client = MagicMock()

    with pytest.raises(ClaimConflict, match="already in-progress"):
        claim_issue(issue, gh_client, conn, "test-bot", run_id)

    # Ensure no API calls were made to modify labels
    issue.remove_from_labels.assert_not_called()
    issue.add_to_labels.assert_not_called()


# ---------------------------------------------------------------------------
# test_claim_raises_conflict_on_github_exception
# ---------------------------------------------------------------------------

def test_claim_raises_conflict_on_github_exception():
    from github import GithubException
    conn = make_conn()
    run_id = insert_run(conn, 3, "API fails")
    issue = make_issue(number=3, label_names=["agent-queue"])
    issue.remove_from_labels.side_effect = GithubException(422, "Unprocessable")
    gh_client = MagicMock()

    with pytest.raises(ClaimConflict, match="Failed to claim"):
        claim_issue(issue, gh_client, conn, "test-bot", run_id)


# ---------------------------------------------------------------------------
# test_release_claim_is_idempotent
# ---------------------------------------------------------------------------

def test_release_claim_removes_in_progress_label():
    conn = make_conn()
    run_id = insert_run(conn, 4, "To release")
    issue = make_issue(number=4, label_names=["in-progress"])
    gh_client = MagicMock()

    # Add a DB claim first
    from bugagent.db import add_claim
    add_claim(conn, 4, run_id)

    release_claim(issue, gh_client, conn, "test-bot")

    issue.remove_from_labels.assert_called_once_with("in-progress")
    issue.remove_from_assignees.assert_called_once_with("test-bot")

    # DB claim should be gone
    row = conn.execute("SELECT * FROM claims WHERE issue_number=4").fetchone()
    assert row is None


def test_release_claim_is_idempotent_no_labels():
    conn = make_conn()
    issue = make_issue(number=5, label_names=[])
    gh_client = MagicMock()

    # Should not raise even without in-progress label or DB entry
    release_claim(issue, gh_client, conn, "test-bot")
    issue.remove_from_labels.assert_not_called()


def test_release_claim_handles_remove_assignee_exception():
    conn = make_conn()
    issue = make_issue(number=6, label_names=["in-progress"])
    issue.remove_from_assignees.side_effect = Exception("network error")
    gh_client = MagicMock()

    # Should not propagate exception
    release_claim(issue, gh_client, conn, "test-bot")
    issue.remove_from_labels.assert_called_once_with("in-progress")
