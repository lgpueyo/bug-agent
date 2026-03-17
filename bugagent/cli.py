import sys
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

import click

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def _load(config_path: str):
    from bugagent.config import load_config
    return load_config(config_path)


def _db(config):
    from bugagent.db import init_db, get_connection
    init_db(config.db_path)
    return get_connection(config.db_path)


@click.group()
def main():
    """Bug-Fix Agent — automated GitHub issue fixer."""
    pass


@main.command()
@click.option("--once", is_flag=True, help="Process queue once and exit (good for cron).")
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def run(once, config_path):
    """Start the watcher loop (daemon mode with internal sleep)."""
    config = _load(config_path)
    conn = _db(config)
    from bugagent.watcher import run_loop
    run_loop(config, conn, once=once)


@main.command()
@click.option("--issue", "issue_number", type=int, default=None, help="Show runs for a specific issue.")
@click.option("--since", default=None, help="Show runs since date (e.g. 'yesterday', ISO date).")
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def status(issue_number, since, config_path):
    """Show recent run summary."""
    config = _load(config_path)
    conn = _db(config)
    from bugagent.db import list_runs

    since_iso = None
    if since == "yesterday":
        since_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    elif since:
        since_iso = since

    runs = list_runs(conn, issue_number=issue_number, since=since_iso)
    if not runs:
        click.echo("No runs found.")
        return

    for r in runs:
        pr_info = f"PR #{r['pr_number']}" if r.get("pr_number") else ""
        click.echo(f"[{r['started_at']}] Issue #{r['issue_number']} — {r['status']} {pr_info}")
        if r.get("error_msg"):
            click.echo(f"  Error: {r['error_msg'][:100]}")


@main.command()
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def setup(config_path):
    """Create required GitHub labels and initialize DB."""
    config = _load(config_path)
    conn = _db(config)

    from github import Github
    gh = Github(config.github_token)
    repo = gh.get_repo(config.repo)

    labels_to_create = [
        ("agent-queue", "0075ca", "Issue is ready for the agent to pick up"),
        ("in-progress", "e4e669", "Agent has claimed this issue and is working"),
        ("needs-human", "d93f0b", "Agent could not fix — human intervention required"),
        ("agent-fix-pr", "0e8a16", "Agent opened a PR; awaiting human review/merge"),
        ("agent-fix-unverified", "f9d0c4", "PR opened but test suite could not be run"),
    ]

    existing = {l.name for l in repo.get_labels()}
    for name, color, desc in labels_to_create:
        if name not in existing:
            repo.create_label(name=name, color=color, description=desc)
            click.echo(f"  Created label: {name}")
        else:
            click.echo(f"  Label already exists: {name}")

    click.echo("Setup complete.")


@main.command()
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def doctor(config_path):
    """Check all dependencies and configuration."""
    import subprocess
    import shutil

    all_ok = True

    def check(label, ok, detail=""):
        nonlocal all_ok
        icon = "v" if ok else "x"
        line = f"{icon}  {label}"
        if detail:
            line += f" ({detail})"
        click.echo(line)
        if not ok:
            all_ok = False

    # GitHub token
    try:
        config = _load(config_path)
        from github import Github
        gh = Github(config.github_token)
        user = gh.get_user()
        _ = user.login
        check("GitHub token valid (repo scope)", True)
    except Exception as e:
        check("GitHub token valid", False, str(e)[:60])

    # gh CLI
    gh_path = shutil.which("gh")
    if gh_path:
        result = subprocess.run(["gh", "--version"], capture_output=True, text=True)
        ver = result.stdout.split("\n")[0] if result.returncode == 0 else "unknown"
        check("gh CLI installed", result.returncode == 0, ver)
    else:
        check("gh CLI installed", False, "not found")

    # Claude Code CLI
    claude_path = shutil.which("claude")
    if claude_path:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True)
        ver = result.stdout.strip() if result.returncode == 0 else "unknown"
        check("Claude Code installed", result.returncode == 0, ver)
    else:
        check("Claude Code installed", False, "not found")

    # Target repo reachable
    try:
        config = _load(config_path)
        from github import Github
        gh = Github(config.github_token)
        repo = gh.get_repo(config.repo)
        check(f"Target repo reachable ({config.repo})", True)
    except Exception as e:
        check("Target repo reachable", False, str(e)[:60])

    # Labels
    try:
        config = _load(config_path)
        from github import Github
        gh = Github(config.github_token)
        repo = gh.get_repo(config.repo)
        existing = {l.name for l in repo.get_labels()}
        required = {"agent-queue", "in-progress", "needs-human", "agent-fix-pr", "agent-fix-unverified"}
        present = required & existing
        check(f"Labels present ({len(present)}/5)", len(present) == 5)
    except Exception as e:
        check("Labels present", False, str(e)[:60])

    # DB schema
    try:
        config = _load(config_path)
        conn = _db(config)
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        ver = row[0] if row else 0
        from bugagent.db import SCHEMA_VERSION
        check(f"DB schema current (migration {ver})", ver >= 1)
    except Exception as e:
        check("DB schema current", False, str(e)[:60])

    # Stale claims
    try:
        config = _load(config_path)
        conn = _db(config)
        from bugagent.db import get_stale_claims
        stale = get_stale_claims(conn, config.claim_timeout_minutes)
        check("No stale claims", len(stale) == 0, f"{len(stale)} stale" if stale else "")
    except Exception as e:
        check("No stale claims", False, str(e)[:60])

    sys.exit(0 if all_ok else 1)


@main.command()
@click.option("--issue", "issue_number", required=True, type=int, help="Issue number to release.")
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def release(issue_number, config_path):
    """Manually release a stuck in-progress claim."""
    config = _load(config_path)
    conn = _db(config)
    from github import Github
    from bugagent.claim import release_claim
    gh = Github(config.github_token)
    repo = gh.get_repo(config.repo)
    issue = repo.get_issue(issue_number)
    release_claim(issue, gh, conn, config.bot_username)
    click.echo(f"Released claim for issue #{issue_number}")


@main.group()
def config():
    """Config management commands."""
    pass


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def config_set(key, value, config_path):
    """Safely update a config value."""
    import yaml
    p = Path(config_path)
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    data[key] = value
    with open(p, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    click.echo(f"Set {key} = {value}")


@config.command("validate")
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
def config_validate(config_path):
    """Validate config.yaml without running."""
    try:
        cfg = _load(config_path)
        click.echo(f"Config valid. Repo: {cfg.repo}")
    except Exception as e:
        click.echo(f"Config invalid: {e}", err=True)
        sys.exit(1)
