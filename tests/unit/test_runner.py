"""Unit tests for bugagent.runner"""
import subprocess
import pytest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

from bugagent.runner import RunResult, _render_prompt, _check_dirty_repo, run_agent
from bugagent.db import apply_migrations, insert_run
import sqlite3


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def make_config(timeout_minutes=1):
    cfg = MagicMock()
    cfg.agent_timeout_minutes = timeout_minutes
    cfg.agent_timeout_seconds = timeout_minutes * 60
    cfg.test_command = "pytest"
    cfg.log_path = "logs/agent.log"
    cfg.repo_path = "/tmp/repo"
    return cfg


def make_issue(number=42, title="Fix the bug", body="The widget explodes."):
    issue = MagicMock()
    issue.number = number
    issue.title = title
    issue.body = body
    return issue


# ---------------------------------------------------------------------------
# test_runner_builds_prompt_with_issue_content
# ---------------------------------------------------------------------------

def test_render_prompt_substitutes_placeholders(tmp_path):
    # Write a temporary template
    template = (
        "Issue #{ISSUE_NUMBER}: {ISSUE_TITLE}\n"
        "{ISSUE_BODY}\n"
        "Run: {TEST_COMMAND}"
    )
    # Patch PROMPT_TEMPLATE_PATH
    with patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_path:
        mock_path.read_text.return_value = template
        result = _render_prompt(
            issue_number=99,
            issue_title="Crash on startup",
            issue_body="App crashes when X happens.",
            test_command="make test",
        )

    assert "Issue #99: Crash on startup" in result
    assert "App crashes when X happens." in result
    assert "Run: make test" in result
    assert "{ISSUE_NUMBER}" not in result
    assert "{ISSUE_TITLE}" not in result
    assert "{ISSUE_BODY}" not in result
    assert "{TEST_COMMAND}" not in result


def test_render_prompt_handles_none_body(tmp_path):
    template = "Body: {ISSUE_BODY}"
    with patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_path:
        mock_path.read_text.return_value = template
        result = _render_prompt(1, "T", None, "pytest")
    assert "(no body provided)" in result


# ---------------------------------------------------------------------------
# test_runner_returns_dirty_repo_alert_on_dirty_repo
# ---------------------------------------------------------------------------

def test_runner_returns_dirty_repo_alert_on_dirty_repo():
    issue = make_issue()
    config = make_config()

    with patch("bugagent.runner._check_dirty_repo", return_value=True):
        result = run_agent(issue, config, "/tmp/repo")

    assert result.success is False
    assert "DIRTY_REPO" in result.alert_codes
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# test_runner_kills_process_on_timeout
# ---------------------------------------------------------------------------

def test_runner_kills_process_on_timeout(tmp_path):
    issue = make_issue()
    config = make_config(timeout_minutes=1)
    config.log_path = str(tmp_path / "logs" / "agent.log")

    mock_proc = MagicMock()
    mock_proc.returncode = -9
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="claude", timeout=60),
        ("timed out output", None),
    ]

    with patch("bugagent.runner._check_dirty_repo", return_value=False), \
         patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_tmpl, \
         patch("bugagent.runner.subprocess.Popen", return_value=mock_proc):
        mock_tmpl.read_text.return_value = "prompt {ISSUE_NUMBER} {ISSUE_TITLE} {ISSUE_BODY} {TEST_COMMAND}"
        result = run_agent(issue, config, str(tmp_path))

    assert result.timed_out is True
    assert result.success is False
    assert "TIMEOUT" in result.alert_codes
    assert result.returncode == -1
    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# test_runner_returns_agent_failed_on_nonzero_exit
# ---------------------------------------------------------------------------

def test_runner_returns_agent_failed_on_nonzero_exit(tmp_path):
    issue = make_issue()
    config = make_config()
    config.log_path = str(tmp_path / "logs" / "agent.log")

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = ("some error output", None)

    with patch("bugagent.runner._check_dirty_repo", return_value=False), \
         patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_tmpl, \
         patch("bugagent.runner.subprocess.Popen", return_value=mock_proc):
        mock_tmpl.read_text.return_value = "prompt {ISSUE_NUMBER} {ISSUE_TITLE} {ISSUE_BODY} {TEST_COMMAND}"
        result = run_agent(issue, config, str(tmp_path))

    assert result.success is False
    assert "AGENT_FAILED" in result.alert_codes
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# test_runner_returns_success_on_zero_exit
# ---------------------------------------------------------------------------

def test_runner_returns_success_on_zero_exit(tmp_path):
    issue = make_issue()
    config = make_config()
    config.log_path = str(tmp_path / "logs" / "agent.log")

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = ("Fixed the bug successfully", None)

    with patch("bugagent.runner._check_dirty_repo", return_value=False), \
         patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_tmpl, \
         patch("bugagent.runner.subprocess.Popen", return_value=mock_proc):
        mock_tmpl.read_text.return_value = "prompt {ISSUE_NUMBER} {ISSUE_TITLE} {ISSUE_BODY} {TEST_COMMAND}"
        result = run_agent(issue, config, str(tmp_path))

    assert result.success is True
    assert result.returncode == 0
    assert result.alert_codes == []


# ---------------------------------------------------------------------------
# test_runner_returns_agent_failed_on_cannot_fix_in_output
# ---------------------------------------------------------------------------

def test_runner_returns_agent_failed_on_cannot_fix_in_output(tmp_path):
    issue = make_issue()
    config = make_config()
    config.log_path = str(tmp_path / "logs" / "agent.log")

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (
        "AGENT_CANNOT_FIX: The bug requires information not in the report", None
    )

    with patch("bugagent.runner._check_dirty_repo", return_value=False), \
         patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_tmpl, \
         patch("bugagent.runner.subprocess.Popen", return_value=mock_proc):
        mock_tmpl.read_text.return_value = "prompt {ISSUE_NUMBER} {ISSUE_TITLE} {ISSUE_BODY} {TEST_COMMAND}"
        result = run_agent(issue, config, str(tmp_path))

    assert result.success is False
    assert "AGENT_FAILED" in result.alert_codes


# ---------------------------------------------------------------------------
# test_runner_returns_missing_alert_when_claude_not_found
# ---------------------------------------------------------------------------

def test_runner_returns_missing_alert_when_claude_not_found(tmp_path):
    issue = make_issue()
    config = make_config()
    config.log_path = str(tmp_path / "logs" / "agent.log")

    with patch("bugagent.runner._check_dirty_repo", return_value=False), \
         patch("bugagent.runner.PROMPT_TEMPLATE_PATH") as mock_tmpl, \
         patch("bugagent.runner.subprocess.Popen", side_effect=FileNotFoundError):
        mock_tmpl.read_text.return_value = "prompt {ISSUE_NUMBER} {ISSUE_TITLE} {ISSUE_BODY} {TEST_COMMAND}"
        result = run_agent(issue, config, str(tmp_path))

    assert result.success is False
    assert "CLAUDE_CODE_MISSING" in result.alert_codes


# ---------------------------------------------------------------------------
# test_check_dirty_repo
# ---------------------------------------------------------------------------

def test_check_dirty_repo_returns_true_when_dirty():
    mock_result = MagicMock()
    mock_result.stdout = " M somefile.py\n"
    with patch("bugagent.runner.subprocess.run", return_value=mock_result):
        assert _check_dirty_repo("/tmp/repo") is True


def test_check_dirty_repo_returns_false_when_clean():
    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch("bugagent.runner.subprocess.run", return_value=mock_result):
        assert _check_dirty_repo("/tmp/repo") is False
