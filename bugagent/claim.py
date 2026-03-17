import logging
from datetime import datetime, timezone
from github import Github, GithubException
from bugagent.db import add_claim, release_claim as db_release_claim, get_stale_claims

log = logging.getLogger(__name__)


class ClaimConflict(Exception):
    pass


def claim_issue(issue, github_client, db_conn, bot_username: str, run_id: int) -> None:
    """Claim an issue: remove agent-queue, add in-progress, assign bot."""
    labels = [l.name for l in issue.labels]
    if "in-progress" in labels:
        raise ClaimConflict(f"Issue #{issue.number} already in-progress")

    try:
        issue.remove_from_labels("agent-queue")
        issue.add_to_labels("in-progress")
        issue.add_to_assignees(bot_username)
    except GithubException as e:
        raise ClaimConflict(f"Failed to claim issue #{issue.number}: {e}")

    add_claim(db_conn, issue.number, run_id)
    log.info(f"Claimed issue #{issue.number}")


def release_claim(issue, github_client, db_conn, bot_username: str) -> None:
    """Release an issue claim (idempotent)."""
    try:
        labels = [l.name for l in issue.labels]
        if "in-progress" in labels:
            issue.remove_from_labels("in-progress")
        # Re-add agent-queue? No — only do so if explicitly retrying
        try:
            issue.remove_from_assignees(bot_username)
        except Exception:
            pass
    except Exception as e:
        log.warning(f"Error releasing claim for issue #{issue.number}: {e}")

    try:
        db_release_claim(db_conn, issue.number)
    except Exception as e:
        log.warning(f"DB error releasing claim for issue #{issue.number}: {e}")


def release_stale_claims(github_client, db_conn, config) -> None:
    """Release any claims with stale heartbeats."""
    stale = get_stale_claims(db_conn, config.claim_timeout_minutes)
    if not stale:
        return

    repo = github_client.get_repo(config.repo)
    for claim in stale:
        issue_number = claim["issue_number"]
        log.warning(f"Releasing stale claim for issue #{issue_number}")
        try:
            issue = repo.get_issue(issue_number)
            release_claim(issue, github_client, db_conn, config.bot_username)
        except Exception as e:
            log.error(f"Failed to release stale claim #{issue_number}: {e}")
