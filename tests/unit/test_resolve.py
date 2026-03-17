"""Unit tests for bugagent.resolve"""
import pytest
import sqlite3
from unittest.mock import MagicMock, patch, call

from bugagent.db import apply_migrations, insert_run
from bugagent.runner import RunResult
from bugagent.verify import VerifyResult
from bugagent.resolve import resolve, _label_needs_human, _extract_cannot_fix_reason, _extract_summary


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def make_config():
    cfg = MagicMock()
    cfg.github_token = "ghp_test"
    cfg.repo = "myorg/myrepo"
    cfg.test_command = "pytest"
    cfg.agent_timeout_minutes = 30
    cfg.log_path = "logs/agent.log"
    return cfg


def make_issue(number=1, state="open", label_names=None):
    issue = MagicMock()
    issue.number = number
    issue.title = f"Test issue {number}"
    issue.body = "This is a bug."
    issue.state = state
    issue.labels = [MagicMock(name=n) for n in (label_names or ["in-progress"])]
    # Simulate update() refreshing state
    issue.update = MagicMock()
    return issue


def make_run_result(success=True, stdout="Fixed!", returncode=0, alert_codes=None, timed_out=False):
    return RunResult(
        success=success,
        stdout=stdout,
        returncode=returncode,
        alert_codes=alert_codes or [],
        timed_out=timed_out,
    )


def make_verify_result(passed=True, output="5 passed", skipped=False, alert_codes=None):
    return VerifyResult(
        passed=passed,
        output_excerpt=output,
        skipped=skipped,
        alert_codes=alert_codes or [],
    )


# Shared git mock: success for everything
def _git_ok(*args, **kwargs):
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# test_resolve_opens_pr_on_success
# ---------------------------------------------------------------------------

def test_resolve_opens_pr_on_success():
    conn = make_conn()
    run_id = insert_run(conn, 1, "Test issue 1")
    issue = make_issue(number=1)
    config = make_config()

    run_result = make_run_result(success=True, stdout="Applied fix to widget.py")
    verify_result = make_verify_result(passed=True)

    mock_pr = MagicMock()
    mock_pr.number = 99

    mock_repo = MagicMock()
    mock_repo.default_branch = "main"
    mock_repo.create_pull.return_value = mock_pr

    with patch("bugagent.resolve._git", side_effect=_git_ok), \
         patch("bugagent.resolve.Github") as MockGithub:
        MockGithub.return_value.get_repo.return_value = mock_repo
        status = resolve(issue, run_result, verify_result, config, "/tmp/repo", conn, run_id)

    assert status == "pr_opened"
    mock_repo.create_pull.assert_called_once()
    call_kwargs = mock_repo.create_pull.call_args[1]
    assert "fix: #1" in call_kwargs["title"]
    assert "main" == call_kwargs["base"]

    # Check DB updated
    from bugagent.db import get_run
    row = get_run(conn, run_id)
    assert row["status"] == "pr_opened"
    assert row["pr_number"] == 99


# ---------------------------------------------------------------------------
# test_resolve_labels_needs_human_on_agent_cannot_fix
# ---------------------------------------------------------------------------

def test_resolve_labels_needs_human_on_agent_cannot_fix():
    conn = make_conn()
    run_id = insert_run(conn, 2, "Test issue 2")
    issue = make_issue(number=2, label_names=["in-progress"])
    config = make_config()

    run_result = make_run_result(
        success=False,
        stdout="AGENT_CANNOT_FIX: Not enough information in the report",
        returncode=0,
        alert_codes=["AGENT_FAILED"],
    )

    with patch("bugagent.resolve._revert_changes"), \
         patch("bugagent.resolve._label_needs_human") as mock_label:
        status = resolve(issue, run_result, None, config, "/tmp/repo", conn, run_id)

    assert status == "needs_human"
    mock_label.assert_called_once()
    args = mock_label.call_args[0]
    assert "Not enough information" in args[1]
    assert args[2] == "AGENT_FAILED"

    from bugagent.db import get_run
    row = get_run(conn, run_id)
    assert row["status"] == "needs_human"


# ---------------------------------------------------------------------------
# test_resolve_labels_needs_human_on_tests_failed
# ---------------------------------------------------------------------------

def test_resolve_labels_needs_human_on_tests_failed():
    conn = make_conn()
    run_id = insert_run(conn, 3, "Test issue 3")
    issue = make_issue(number=3, label_names=["in-progress"])
    config = make_config()

    run_result = make_run_result(success=True, stdout="Fixed!")
    verify_result = make_verify_result(
        passed=False,
        output="3 failed",
        alert_codes=["TESTS_FAILED"],
    )

    with patch("bugagent.resolve._revert_changes"), \
         patch("bugagent.resolve._label_needs_human") as mock_label:
        status = resolve(issue, run_result, verify_result, config, "/tmp/repo", conn, run_id)

    assert status == "needs_human"
    mock_label.assert_called_once()
    args = mock_label.call_args[0]
    assert args[2] == "TESTS_FAILED"


# ---------------------------------------------------------------------------
# test_resolve_labels_needs_human_on_timeout
# ---------------------------------------------------------------------------

def test_resolve_labels_needs_human_on_timeout():
    conn = make_conn()
    run_id = insert_run(conn, 4, "Test issue 4")
    issue = make_issue(number=4, label_names=["in-progress"])
    config = make_config()

    run_result = make_run_result(
        success=False,
        stdout="",
        returncode=-1,
        alert_codes=["TIMEOUT"],
        timed_out=True,
    )

    with patch("bugagent.resolve._revert_changes"), \
         patch("bugagent.resolve._label_needs_human") as mock_label:
        status = resolve(issue, run_result, None, config, "/tmp/repo", conn, run_id)

    assert status == "needs_human"
    mock_label.assert_called_once()
    args = mock_label.call_args[0]
    assert "TIMEOUT" == args[2]


# ---------------------------------------------------------------------------
# test_resolve_reverts_changes_on_all_failure_paths
# ---------------------------------------------------------------------------

def test_resolve_reverts_changes_on_agent_failed():
    conn = make_conn()
    run_id = insert_run(conn, 5, "Test issue 5")
    issue = make_issue(number=5)
    config = make_config()

    run_result = make_run_result(
        success=False,
        stdout="Something went wrong",
        returncode=1,
        alert_codes=["AGENT_FAILED"],
    )

    with patch("bugagent.resolve._revert_changes") as mock_revert, \
         patch("bugagent.resolve._label_needs_human"):
        resolve(issue, run_result, None, config, "/tmp/repo", conn, run_id)

    mock_revert.assert_called_once_with("/tmp/repo")


def test_resolve_reverts_changes_on_tests_failed():
    conn = make_conn()
    run_id = insert_run(conn, 6, "Test issue 6")
    issue = make_issue(number=6)
    config = make_config()

    run_result = make_run_result(success=True)
    verify_result = make_verify_result(passed=False, alert_codes=["TESTS_FAILED"])

    with patch("bugagent.resolve._revert_changes") as mock_revert, \
         patch("bugagent.resolve._label_needs_human"):
        resolve(issue, run_result, verify_result, config, "/tmp/repo", conn, run_id)

    mock_revert.assert_called_once_with("/tmp/repo")


def test_resolve_reverts_changes_on_timeout():
    conn = make_conn()
    run_id = insert_run(conn, 7, "Test issue 7")
    issue = make_issue(number=7)
    config = make_config()

    run_result = make_run_result(success=False, timed_out=True, alert_codes=["TIMEOUT"])

    with patch("bugagent.resolve._revert_changes") as mock_revert, \
         patch("bugagent.resolve._label_needs_human"):
        resolve(issue, run_result, None, config, "/tmp/repo", conn, run_id)

    mock_revert.assert_called_once_with("/tmp/repo")


# ---------------------------------------------------------------------------
# test_resolve_skips_pr_if_issue_closed_during_run
# ---------------------------------------------------------------------------

def test_resolve_skips_pr_if_issue_closed_during_run():
    conn = make_conn()
    run_id = insert_run(conn, 8, "Test issue 8")
    issue = make_issue(number=8)
    issue.state = "closed"
    config = make_config()

    run_result = make_run_result(success=True)
    verify_result = make_verify_result(passed=True)

    with patch("bugagent.resolve._revert_changes") as mock_revert, \
         patch("bugagent.resolve._git", side_effect=_git_ok):
        status = resolve(issue, run_result, verify_result, config, "/tmp/repo", conn, run_id)

    assert status == "skipped"
    mock_revert.assert_called_once()


# ---------------------------------------------------------------------------
# helper function tests
# ---------------------------------------------------------------------------

def test_extract_cannot_fix_reason():
    stdout = "Some output\nAGENT_CANNOT_FIX: Need more details\nMore output"
    assert _extract_cannot_fix_reason(stdout) == "Need more details"


def test_extract_cannot_fix_reason_returns_unknown_when_absent():
    assert _extract_cannot_fix_reason("no marker here") == "Unknown reason"


def test_extract_summary_filters_short_lines():
    stdout = "line one\nline two\n[bracketed line]\nshort"
    result = _extract_summary(stdout)
    assert "[bracketed line]" not in result
    assert "line one" in result or "line two" in result
