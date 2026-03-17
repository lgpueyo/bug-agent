"""Integration tests for the full bug-fix agent cycle."""
import sqlite3
import pytest
from unittest.mock import MagicMock, patch, call
from pathlib import Path

from bugagent.db import apply_migrations, insert_run, get_run
from bugagent.runner import RunResult
from bugagent.verify import VerifyResult
from bugagent.watcher import run_once


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def make_config():
    cfg = MagicMock()
    cfg.github_token = "ghp_test"
    cfg.repo = "myorg/myrepo"
    cfg.bot_username = "test-bot"
    cfg.repo_path = "/tmp/fake-repo"
    cfg.test_command = "pytest"
    cfg.agent_timeout_minutes = 30
    cfg.agent_timeout_seconds = 1800
    cfg.claim_timeout_minutes = 45
    cfg.log_path = "logs/agent.log"
    cfg.poll_interval_seconds = 300
    return cfg


def make_github_issue(number=1, title="Fix widget crash", body="Widget explodes on input X.", label_names=None):
    issue = MagicMock()
    issue.number = number
    issue.title = title
    issue.body = body
    issue.state = "open"
    if label_names is None:
        label_names = ["agent-queue"]
    label_mocks = []
    for n in label_names:
        lbl = MagicMock()
        lbl.name = n
        label_mocks.append(lbl)
    issue.labels = label_mocks
    issue.update = MagicMock()
    return issue


def _git_ok(*args, **kwargs):
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# test_full_cycle_success
# ---------------------------------------------------------------------------

def test_full_cycle_success():
    """
    Full happy path: issue in queue -> claim -> agent fixes it -> tests pass -> PR opened.
    """
    conn = make_conn()
    config = make_config()
    issue = make_github_issue(number=10)

    mock_pr = MagicMock()
    mock_pr.number = 55

    mock_gh_repo = MagicMock()
    mock_gh_repo.default_branch = "main"
    mock_gh_repo.create_pull.return_value = mock_pr

    mock_gh_instance = MagicMock()
    mock_gh_instance.get_repo.return_value = mock_gh_repo
    mock_gh_repo.get_issues.return_value = [issue]

    success_run_result = RunResult(success=True, stdout="Applied fix to widget.py", returncode=0)
    success_verify_result = VerifyResult(passed=True, output_excerpt="5 passed")

    with patch("bugagent.watcher.Github", return_value=mock_gh_instance), \
         patch("bugagent.watcher.release_stale_claims"), \
         patch("bugagent.claim.add_claim") as mock_add_claim, \
         patch("bugagent.runner.run_agent", return_value=success_run_result), \
         patch("bugagent.verify.run_tests", return_value=success_verify_result), \
         patch("bugagent.resolve._git", side_effect=_git_ok), \
         patch("bugagent.resolve.Github", return_value=mock_gh_instance):

        run_once(config, conn)

    # PR should have been created
    mock_gh_repo.create_pull.assert_called_once()
    call_kwargs = mock_gh_repo.create_pull.call_args[1]
    assert "#10" in call_kwargs["title"]

    # Check DB: there should be a run with status pr_opened
    runs = conn.execute("SELECT * FROM runs WHERE issue_number=10").fetchall()
    assert len(runs) >= 1
    final_run = dict(runs[0])
    assert final_run["status"] == "pr_opened"
    assert final_run["pr_number"] == 55


# ---------------------------------------------------------------------------
# test_full_cycle_agent_cannot_fix
# ---------------------------------------------------------------------------

def test_full_cycle_agent_cannot_fix():
    """
    Agent returns AGENT_CANNOT_FIX -> issue gets needs-human label.
    """
    conn = make_conn()
    config = make_config()
    issue = make_github_issue(number=20)

    mock_gh_repo = MagicMock()
    mock_gh_repo.get_issues.return_value = [issue]

    mock_gh_instance = MagicMock()
    mock_gh_instance.get_repo.return_value = mock_gh_repo

    cannot_fix_result = RunResult(
        success=False,
        stdout="AGENT_CANNOT_FIX: The bug requires information not present in the report",
        returncode=0,
        alert_codes=["AGENT_FAILED"],
    )

    with patch("bugagent.watcher.Github", return_value=mock_gh_instance), \
         patch("bugagent.watcher.release_stale_claims"), \
         patch("bugagent.claim.add_claim"), \
         patch("bugagent.runner.run_agent", return_value=cannot_fix_result), \
         patch("bugagent.resolve._revert_changes"), \
         patch("bugagent.resolve._label_needs_human") as mock_label, \
         patch("bugagent.resolve.Github", return_value=mock_gh_instance):

        run_once(config, conn)

    mock_label.assert_called()
    call_args = mock_label.call_args[0]
    assert "AGENT_FAILED" == call_args[2]

    runs = conn.execute("SELECT * FROM runs WHERE issue_number=20").fetchall()
    assert len(runs) >= 1
    final_run = dict(runs[0])
    assert final_run["status"] == "needs_human"


# ---------------------------------------------------------------------------
# test_full_cycle_timeout
# ---------------------------------------------------------------------------

def test_full_cycle_timeout():
    """
    Agent times out -> needs-human with TIMEOUT alert code.
    """
    conn = make_conn()
    config = make_config()
    issue = make_github_issue(number=30)

    mock_gh_repo = MagicMock()
    mock_gh_repo.get_issues.return_value = [issue]

    mock_gh_instance = MagicMock()
    mock_gh_instance.get_repo.return_value = mock_gh_repo

    timeout_result = RunResult(
        success=False,
        stdout="",
        returncode=-1,
        alert_codes=["TIMEOUT"],
        timed_out=True,
    )

    with patch("bugagent.watcher.Github", return_value=mock_gh_instance), \
         patch("bugagent.watcher.release_stale_claims"), \
         patch("bugagent.claim.add_claim"), \
         patch("bugagent.runner.run_agent", return_value=timeout_result), \
         patch("bugagent.resolve._revert_changes"), \
         patch("bugagent.resolve._label_needs_human") as mock_label, \
         patch("bugagent.resolve.Github", return_value=mock_gh_instance):

        run_once(config, conn)

    mock_label.assert_called()
    call_args = mock_label.call_args[0]
    assert "TIMEOUT" == call_args[2]

    runs = conn.execute("SELECT * FROM runs WHERE issue_number=30").fetchall()
    assert len(runs) >= 1
    final_run = dict(runs[0])
    assert final_run["status"] == "needs_human"
    assert "TIMEOUT" in final_run["alert_codes"]


# ---------------------------------------------------------------------------
# test_full_cycle_tests_fail
# ---------------------------------------------------------------------------

def test_full_cycle_tests_fail():
    """
    Agent succeeds but test suite fails -> needs-human with TESTS_FAILED.
    """
    conn = make_conn()
    config = make_config()
    issue = make_github_issue(number=40)

    mock_gh_repo = MagicMock()
    mock_gh_repo.get_issues.return_value = [issue]

    mock_gh_instance = MagicMock()
    mock_gh_instance.get_repo.return_value = mock_gh_repo

    success_run = RunResult(success=True, stdout="Fixed!", returncode=0)
    failed_verify = VerifyResult(
        passed=False,
        output_excerpt="3 failed, 2 passed",
        alert_codes=["TESTS_FAILED"],
    )

    with patch("bugagent.watcher.Github", return_value=mock_gh_instance), \
         patch("bugagent.watcher.release_stale_claims"), \
         patch("bugagent.claim.add_claim"), \
         patch("bugagent.runner.run_agent", return_value=success_run), \
         patch("bugagent.verify.run_tests", return_value=failed_verify), \
         patch("bugagent.resolve._revert_changes"), \
         patch("bugagent.resolve._label_needs_human") as mock_label, \
         patch("bugagent.resolve.Github", return_value=mock_gh_instance):

        run_once(config, conn)

    mock_label.assert_called()
    call_args = mock_label.call_args[0]
    assert "TESTS_FAILED" == call_args[2]

    runs = conn.execute("SELECT * FROM runs WHERE issue_number=40").fetchall()
    assert len(runs) >= 1
    final_run = dict(runs[0])
    assert final_run["status"] == "needs_human"


# ---------------------------------------------------------------------------
# test_full_cycle_no_queued_issues
# ---------------------------------------------------------------------------

def test_full_cycle_no_queued_issues():
    """
    When no issues are queued, run_once returns without creating any runs.
    """
    conn = make_conn()
    config = make_config()

    mock_gh_repo = MagicMock()
    mock_gh_repo.get_issues.return_value = []

    mock_gh_instance = MagicMock()
    mock_gh_instance.get_repo.return_value = mock_gh_repo

    with patch("bugagent.watcher.Github", return_value=mock_gh_instance), \
         patch("bugagent.watcher.release_stale_claims"), \
         patch("bugagent.resolve.Github", return_value=mock_gh_instance):
        run_once(config, conn)

    runs = conn.execute("SELECT * FROM runs").fetchall()
    assert len(runs) == 0


# ---------------------------------------------------------------------------
# test_full_cycle_claim_conflict_skips_issue
# ---------------------------------------------------------------------------

def test_full_cycle_claim_conflict_skips_issue():
    """
    If claiming an issue raises ClaimConflict, the run is marked skipped.
    """
    conn = make_conn()
    config = make_config()
    issue = make_github_issue(number=50, label_names=["agent-queue", "in-progress"])

    mock_gh_repo = MagicMock()
    mock_gh_repo.get_issues.return_value = [issue]

    mock_gh_instance = MagicMock()
    mock_gh_instance.get_repo.return_value = mock_gh_repo

    with patch("bugagent.watcher.Github", return_value=mock_gh_instance), \
         patch("bugagent.watcher.release_stale_claims"), \
         patch("bugagent.resolve.Github", return_value=mock_gh_instance):
        run_once(config, conn)

    runs = conn.execute("SELECT * FROM runs WHERE issue_number=50").fetchall()
    assert len(runs) >= 1
    final_run = dict(runs[0])
    assert final_run["status"] == "skipped"
