import subprocess
import logging
import shutil
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger(__name__)

MAX_OUTPUT_LINES = 50


@dataclass
class VerifyResult:
    passed: bool
    output_excerpt: str
    alert_codes: List[str] = field(default_factory=list)
    skipped: bool = False


def run_tests(config, repo_path: str) -> VerifyResult:
    """Run the configured test command and return a VerifyResult."""
    cmd_parts = config.test_command.split()
    if not shutil.which(cmd_parts[0]):
        log.warning(f"Test command not found: {cmd_parts[0]}")
        return VerifyResult(passed=False, output_excerpt="", alert_codes=["TEST_CMD_MISSING"], skipped=True)

    try:
        result = subprocess.run(
            config.test_command,
            shell=True,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=config.agent_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(passed=False, output_excerpt="Test suite timed out.",
                            alert_codes=["TIMEOUT"])
    except FileNotFoundError:
        return VerifyResult(passed=False, output_excerpt="", alert_codes=["TEST_CMD_MISSING"], skipped=True)

    combined = result.stdout + result.stderr
    lines = combined.splitlines()
    excerpt = "\n".join(lines[-MAX_OUTPUT_LINES:])

    if result.returncode == 0:
        return VerifyResult(passed=True, output_excerpt=excerpt)
    else:
        return VerifyResult(passed=False, output_excerpt=excerpt, alert_codes=["TESTS_FAILED"])
