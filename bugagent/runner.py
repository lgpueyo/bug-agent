import subprocess
import threading
import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "fix_issue.md"


@dataclass
class RunResult:
    success: bool
    stdout: str
    returncode: int
    alert_codes: List[str] = field(default_factory=list)
    timed_out: bool = False


def _render_prompt(issue_number: int, issue_title: str, issue_body: str, test_command: str) -> str:
    template = PROMPT_TEMPLATE_PATH.read_text()
    return (
        template
        .replace("{ISSUE_NUMBER}", str(issue_number))
        .replace("{ISSUE_TITLE}", issue_title)
        .replace("{ISSUE_BODY}", issue_body or "(no body provided)")
        .replace("{TEST_COMMAND}", test_command)
    )


def _check_dirty_repo(repo_path: str) -> bool:
    """Returns True if repo has uncommitted changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def run_agent(issue, config, repo_path: str, db_conn=None, run_id: int = None) -> RunResult:
    """Run Claude Code agent on the issue. Returns RunResult."""
    # Check for dirty repo
    if _check_dirty_repo(repo_path):
        log.error(f"Dirty repo detected before running agent for issue #{issue.number}")
        return RunResult(success=False, stdout="", returncode=1, alert_codes=["DIRTY_REPO"])

    prompt = _render_prompt(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body or "",
        test_command=config.test_command,
    )

    log_path = Path(config.log_path).parent / f"agent_issue_{issue.number}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "claude",
        "--print",
        "--allowedTools", "Edit,Bash,Read,Write,Glob,Grep",
        "--system-prompt", prompt,
        "Fix the bug described in the system prompt.",
    ]

    log.info(f"Spawning Claude Code for issue #{issue.number} (timeout: {config.agent_timeout_minutes} min)")

    timed_out = False

    try:
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Heartbeat thread
            heartbeat_stop = threading.Event()
            def heartbeat():
                import time
                while not heartbeat_stop.wait(60):
                    if db_conn and run_id:
                        from bugagent.db import update_run
                        from datetime import datetime, timezone
                        update_run(db_conn, run_id, heartbeat_at=datetime.now(timezone.utc).isoformat())

            hb_thread = threading.Thread(target=heartbeat, daemon=True)
            hb_thread.start()

            try:
                stdout, _ = proc.communicate(timeout=config.agent_timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate()
                timed_out = True
            finally:
                heartbeat_stop.set()

            log_file.write(stdout or "")

    except FileNotFoundError:
        return RunResult(success=False, stdout="", returncode=1, alert_codes=["CLAUDE_CODE_MISSING"])

    if db_conn and run_id:
        from bugagent.db import update_run
        update_run(db_conn, run_id, agent_log_path=str(log_path))

    if timed_out:
        return RunResult(success=False, stdout=stdout or "", returncode=-1,
                         alert_codes=["TIMEOUT"], timed_out=True)

    full_stdout = stdout or ""
    if proc.returncode != 0:
        return RunResult(success=False, stdout=full_stdout, returncode=proc.returncode,
                         alert_codes=["AGENT_FAILED"])

    if "AGENT_CANNOT_FIX:" in full_stdout:
        return RunResult(success=False, stdout=full_stdout, returncode=0,
                         alert_codes=["AGENT_FAILED"])

    return RunResult(success=True, stdout=full_stdout, returncode=0)
