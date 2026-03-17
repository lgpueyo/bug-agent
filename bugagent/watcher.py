import os
import sys
import time
import logging
import signal
from pathlib import Path
from github import Github, GithubException
from bugagent.claim import claim_issue, release_claim, release_stale_claims, ClaimConflict

log = logging.getLogger(__name__)

PID_FILE = Path(os.environ.get("TEMP", "/tmp")) / "bugagent.pid"


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _check_pid_lock() -> bool:
    """Returns True if another instance is running."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return True  # Process exists
    except (ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False
    except ValueError:
        PID_FILE.unlink(missing_ok=True)
        return False


def _cleanup_pid():
    PID_FILE.unlink(missing_ok=True)


def run_once(config, db_conn) -> None:
    """Process the queue once."""
    from bugagent.runner import run_agent
    from bugagent.verify import run_tests
    from bugagent.resolve import resolve
    from bugagent.db import insert_run, update_run
    from datetime import datetime, timezone

    gh = Github(config.github_token)
    repo = gh.get_repo(config.repo)

    # Release stale claims first
    release_stale_claims(gh, db_conn, config)

    log.info("Polling for queued issues...")
    try:
        issues = list(repo.get_issues(
            state="open",
            labels=["agent-queue"],
            assignee="none",
            sort="created",
            direction="asc",
        ))
    except GithubException as e:
        log.error(f"GitHub API error fetching issues: {e} [GITHUB_API_ERROR]")
        return

    if not issues:
        log.info("No queued issues found.")
        return

    for issue in issues:
        log.info(f"Found issue #{issue.number}: \"{issue.title}\"")
        run_id = insert_run(db_conn, issue.number, issue.title)
        claimed = False
        try:
            log.info(f"Claiming issue #{issue.number}...")
            claim_issue(issue, gh, db_conn, config.bot_username, run_id)
            claimed = True

            log.info(f"Running Claude Code agent (timeout: {config.agent_timeout_minutes} min)...")
            run_result = run_agent(issue, config, config.repo_path, db_conn, run_id)
            log.info(f"Agent exited: returncode={run_result.returncode}")

            verify_result = None
            if run_result.success:
                log.info("Running test suite...")
                verify_result = run_tests(config, config.repo_path)
                if verify_result.passed:
                    log.info("Tests passed.")
                elif verify_result.skipped:
                    log.warning("Tests skipped (command not found).")
                else:
                    log.warning("Tests FAILED.")

            status = resolve(issue, run_result, verify_result, config, config.repo_path, db_conn, run_id)
            log.info(f"Issue #{issue.number} resolved. Status: {status}")

        except ClaimConflict as e:
            log.info(f"Claim conflict for issue #{issue.number}: {e}")
            from bugagent.db import update_run
            update_run(db_conn, run_id, status="skipped", finished_at=datetime.now(timezone.utc).isoformat())
            continue
        except Exception as e:
            log.error(f"Unexpected error processing issue #{issue.number}: {e}", exc_info=True)
            from bugagent.db import update_run
            update_run(db_conn, run_id, status="error",
                       finished_at=datetime.now(timezone.utc).isoformat(),
                       error_msg=str(e)[:500])
            if claimed:
                try:
                    release_claim(issue, gh, db_conn, config.bot_username)
                except Exception:
                    pass
        finally:
            if claimed:
                try:
                    release_claim(issue, gh, db_conn, config.bot_username)
                except Exception:
                    pass


def run_loop(config, db_conn, once: bool = False) -> None:
    """Main watcher loop."""
    if _check_pid_lock():
        log.error("Another bugagent watcher is already running. [ALREADY_RUNNING]")
        sys.exit(1)

    _write_pid()
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup_pid(), sys.exit(0)))

    try:
        if once:
            run_once(config, db_conn)
        else:
            while True:
                run_once(config, db_conn)
                log.info(f"Sleeping {config.poll_interval_seconds}s...")
                time.sleep(config.poll_interval_seconds)
    finally:
        _cleanup_pid()
