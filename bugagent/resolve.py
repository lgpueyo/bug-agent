import subprocess
import logging
import os
from pathlib import Path
from github import Github, GithubException

log = logging.getLogger(__name__)


def _git(args: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _revert_changes(repo_path: str) -> None:
    _git(["checkout", "."], repo_path)
    _git(["clean", "-fd"], repo_path)


def _extract_summary(stdout: str) -> str:
    """Extract a brief summary from agent stdout."""
    lines = stdout.splitlines()
    # Look for lines that seem like a summary
    summary_lines = [l for l in lines if l.strip() and not l.startswith("[") and len(l) < 200]
    return "\n".join(summary_lines[-10:]) if summary_lines else "(no summary available)"


def _extract_cannot_fix_reason(stdout: str) -> str:
    for line in stdout.splitlines():
        if "AGENT_CANNOT_FIX:" in line:
            return line.split("AGENT_CANNOT_FIX:", 1)[1].strip()
    return "Unknown reason"


def resolve(issue, run_result, verify_result, config, repo_path: str, db_conn, run_id: int) -> str:
    """
    Implement the decision tree from the spec.
    Returns status string: 'pr_opened', 'needs_human', 'error', 'skipped'
    """
    from bugagent.db import update_run
    from datetime import datetime, timezone

    def finish(status: str, alert_codes: list = None, pr_number: int = None, error_msg: str = None):
        now = datetime.now(timezone.utc).isoformat()
        update_run(db_conn, run_id,
                   finished_at=now,
                   status=status,
                   alert_codes=alert_codes or [],
                   pr_number=pr_number,
                   error_msg=error_msg)
        return status

    # --- Success path ---
    if run_result.success and (verify_result is None or verify_result.passed):
        # Check if issue was closed during run
        issue.update()
        if issue.state == "closed":
            log.warning(f"Issue #{issue.number} was closed during run; skipping PR")
            _revert_changes(repo_path)
            return finish("skipped", alert_codes=["ISSUE_CLOSED_DURING_RUN"])

        # Commit and push
        branch = f"agent/fix-{issue.number}"
        _git(["checkout", "-b", branch], repo_path)
        _git(["add", "-A"], repo_path)
        commit_msg = f"fix: resolve issue #{issue.number}\n\n{issue.title}"
        _git(["commit", "-m", commit_msg], repo_path)
        push_result = _git(["push", "-u", "origin", branch], repo_path)

        if push_result.returncode != 0:
            log.error(f"Push failed: {push_result.stderr}")
            _revert_changes(repo_path)
            _label_needs_human(issue, f"Push failed: {push_result.stderr[:200]}", "AGENT_FAILED")
            return finish("needs_human", alert_codes=["AGENT_FAILED"], error_msg=push_result.stderr[:500])

        # Build PR body
        summary = _extract_summary(run_result.stdout)
        test_result_line = "✅ All tests passed" if (verify_result and verify_result.passed) else "⚠️ Tests not run"
        pr_body = f"""## Agent fix for #{issue.number}

Closes #{issue.number}

### What changed
{summary}

### Verification
Test suite: `{config.test_command}`
Result: {test_result_line}

---
*This PR was opened automatically by the Bug-Fix Agent.*
*Please review the diff before merging.*
"""
        # Open PR via GitHub API
        gh = Github(config.github_token)
        repo = gh.get_repo(config.repo)

        try:
            pr = repo.create_pull(
                title=f"fix: #{issue.number} {issue.title}",
                body=pr_body,
                head=branch,
                base=repo.default_branch,
            )
            issue.remove_from_labels("in-progress")
            issue.add_to_labels("agent-fix-pr")
            log.info(f"Opened PR #{pr.number} for issue #{issue.number}")
            return finish("pr_opened", pr_number=pr.number)

        except GithubException as e:
            log.error(f"PR creation failed: {e}")
            # Pending-patch fallback
            patch_dir = Path("logs/pending-prs")
            patch_dir.mkdir(parents=True, exist_ok=True)
            patch_path = patch_dir / f"issue-{issue.number}.patch"
            patch_result = _git(["format-patch", "HEAD~1", "--stdout"], repo_path)
            patch_path.write_text(patch_result.stdout)
            log.warning(f"Saved patch to {patch_path}")
            _label_needs_human(issue, f"PR creation failed: {e}. Patch saved to {patch_path}", "PR_CREATE_FAILED")
            return finish("needs_human", alert_codes=["PR_CREATE_FAILED"], error_msg=str(e))

    # --- Failure paths ---
    _revert_changes(repo_path)

    if run_result.timed_out:
        reason = f"Agent timed out after {config.agent_timeout_minutes} minutes."
        _label_needs_human(issue, reason, "TIMEOUT")
        return finish("needs_human", alert_codes=["TIMEOUT"], error_msg=reason)

    if "AGENT_FAILED" in run_result.alert_codes:
        if "AGENT_CANNOT_FIX:" in run_result.stdout:
            reason = _extract_cannot_fix_reason(run_result.stdout)
        else:
            lines = run_result.stdout.splitlines()
            reason = "\n".join(lines[-20:])
        _label_needs_human(issue, reason, "AGENT_FAILED")
        return finish("needs_human", alert_codes=["AGENT_FAILED"], error_msg=reason[:500])

    if verify_result and not verify_result.passed and not verify_result.skipped:
        reason = f"Tests failed after agent fix:\n```\n{verify_result.output_excerpt[-1000:]}\n```"
        _label_needs_human(issue, reason, "TESTS_FAILED")
        return finish("needs_human", alert_codes=["TESTS_FAILED"])

    if verify_result and verify_result.skipped:
        # Open PR with unverified label
        branch = f"agent/fix-{issue.number}"
        _git(["checkout", "-b", branch], repo_path)
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", f"fix: resolve issue #{issue.number} (unverified)\n\n{issue.title}"], repo_path)
        _git(["push", "-u", "origin", branch], repo_path)
        gh = Github(config.github_token)
        repo_obj = gh.get_repo(config.repo)
        pr_body = f"""## Agent fix for #{issue.number}

Closes #{issue.number}

### What changed
{_extract_summary(run_result.stdout)}

### Verification
⚠️ Test suite could not be run (`{config.test_command}` not found). Please review manually.

---
*This PR was opened automatically by the Bug-Fix Agent.*
"""
        try:
            pr = repo_obj.create_pull(
                title=f"fix: #{issue.number} {issue.title} (unverified)",
                body=pr_body,
                head=branch,
                base=repo_obj.default_branch,
            )
            issue.remove_from_labels("in-progress")
            issue.add_to_labels("agent-fix-unverified")
            return finish("pr_opened", alert_codes=["TEST_CMD_MISSING"], pr_number=pr.number)
        except GithubException as e:
            _label_needs_human(issue, f"PR creation failed: {e}", "PR_CREATE_FAILED")
            return finish("needs_human", alert_codes=["PR_CREATE_FAILED"], error_msg=str(e))

    # Generic failure
    reason = (run_result.stdout or "")[-500:]
    _label_needs_human(issue, reason or "Unknown failure", "AGENT_FAILED")
    return finish("needs_human", alert_codes=["AGENT_FAILED"], error_msg=reason)


def _label_needs_human(issue, reason: str, alert_code: str) -> None:
    comment = f"""🤖 **Agent could not fix this issue automatically.**

**Reason:** {reason}

**Alert code:** `{alert_code}`

**What to do:**
1. Review the issue description — it may need more detail.
2. Fix manually, or remove the `needs-human` label and re-add
   `agent-queue` to retry with a more detailed description.
"""
    try:
        labels = [l.name for l in issue.labels]
        if "in-progress" in labels:
            issue.remove_from_labels("in-progress")
        issue.add_to_labels("needs-human")
        issue.create_comment(comment)
    except Exception as e:
        log.error(f"Failed to label needs-human for issue #{issue.number}: {e}")
