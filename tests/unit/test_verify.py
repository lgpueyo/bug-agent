"""Unit tests for bugagent.verify"""
import subprocess
import pytest
from unittest.mock import MagicMock, patch

from bugagent.verify import run_tests, VerifyResult


def make_config(test_command="pytest", timeout_seconds=1800):
    cfg = MagicMock()
    cfg.test_command = test_command
    cfg.agent_timeout_seconds = timeout_seconds
    return cfg


# ---------------------------------------------------------------------------
# test_verify_returns_pass_on_zero_exit_code
# ---------------------------------------------------------------------------

def test_verify_returns_pass_on_zero_exit_code():
    config = make_config()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "5 passed\n"
    mock_result.stderr = ""

    with patch("bugagent.verify.shutil.which", return_value="/usr/bin/pytest"), \
         patch("bugagent.verify.subprocess.run", return_value=mock_result):
        result = run_tests(config, "/tmp/repo")

    assert result.passed is True
    assert result.skipped is False
    assert result.alert_codes == []
    assert "5 passed" in result.output_excerpt


# ---------------------------------------------------------------------------
# test_verify_returns_fail_on_nonzero_exit_code
# ---------------------------------------------------------------------------

def test_verify_returns_fail_on_nonzero_exit_code():
    config = make_config()
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "3 failed, 2 passed\n"
    mock_result.stderr = "AssertionError: expected X got Y\n"

    with patch("bugagent.verify.shutil.which", return_value="/usr/bin/pytest"), \
         patch("bugagent.verify.subprocess.run", return_value=mock_result):
        result = run_tests(config, "/tmp/repo")

    assert result.passed is False
    assert result.skipped is False
    assert "TESTS_FAILED" in result.alert_codes
    assert "3 failed" in result.output_excerpt


# ---------------------------------------------------------------------------
# test_verify_returns_skipped_when_command_missing
# ---------------------------------------------------------------------------

def test_verify_returns_skipped_when_command_missing():
    config = make_config(test_command="nonexistent-test-runner")

    with patch("bugagent.verify.shutil.which", return_value=None):
        result = run_tests(config, "/tmp/repo")

    assert result.passed is False
    assert result.skipped is True
    assert "TEST_CMD_MISSING" in result.alert_codes


# ---------------------------------------------------------------------------
# test_verify_returns_timeout_alert
# ---------------------------------------------------------------------------

def test_verify_returns_timeout_alert():
    config = make_config()

    with patch("bugagent.verify.shutil.which", return_value="/usr/bin/pytest"), \
         patch("bugagent.verify.subprocess.run", side_effect=subprocess.TimeoutExpired("pytest", 60)):
        result = run_tests(config, "/tmp/repo")

    assert result.passed is False
    assert "TIMEOUT" in result.alert_codes
    assert "timed out" in result.output_excerpt.lower()


# ---------------------------------------------------------------------------
# test_verify_truncates_long_output_to_max_lines
# ---------------------------------------------------------------------------

def test_verify_truncates_long_output_to_max_lines():
    config = make_config()
    long_output = "\n".join([f"line {i}" for i in range(200)])
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = long_output
    mock_result.stderr = ""

    with patch("bugagent.verify.shutil.which", return_value="/usr/bin/pytest"), \
         patch("bugagent.verify.subprocess.run", return_value=mock_result):
        result = run_tests(config, "/tmp/repo")

    # Should only have the last MAX_OUTPUT_LINES (50) lines
    lines = result.output_excerpt.splitlines()
    assert len(lines) <= 50
    assert "line 199" in result.output_excerpt


# ---------------------------------------------------------------------------
# test_verify_handles_file_not_found_gracefully
# ---------------------------------------------------------------------------

def test_verify_handles_file_not_found_gracefully():
    config = make_config(test_command="custom-runner args")

    # shutil.which returns something, but the subprocess raises FileNotFoundError
    with patch("bugagent.verify.shutil.which", return_value="/usr/local/bin/custom-runner"), \
         patch("bugagent.verify.subprocess.run", side_effect=FileNotFoundError):
        result = run_tests(config, "/tmp/repo")

    assert result.skipped is True
    assert "TEST_CMD_MISSING" in result.alert_codes
