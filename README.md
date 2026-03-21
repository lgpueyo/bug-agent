# bug-agent

An autonomous GitHub issue fixer powered by [Claude Code](https://claude.ai/claude-code). bug-agent monitors your repository for issues labeled `agent-queue`, spawns Claude Code to fix them, verifies the fix with your test suite, and opens a pull request — or escalates to humans when automation reaches its limits.

## How it works

1. **Poll** — Watch for GitHub issues labeled `agent-queue`
2. **Claim** — Mark the issue `in-progress` and assign the bot
3. **Fix** — Run Claude Code with the issue details as context
4. **Verify** — Execute your configured test command
5. **Resolve** — Open a PR on success, or label `needs-human` with diagnostics on failure

## Requirements

- Python ≥ 3.11
- [Claude Code CLI](https://claude.ai/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`)
- `git`

## Installation

```bash
pip install -e .
# or
make install
```

## Setup

```bash
# Copy and edit the config template
cp config.example.yaml config.yaml

# Create GitHub labels and initialize the database
bugagent setup --config config.yaml

# Verify all dependencies and config
bugagent doctor --config config.yaml
```

## Configuration

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `github_token` | yes | — | GitHub PAT with `repo` scope |
| `repo` | yes | — | Target repo (`org/repo`) |
| `bot_username` | yes | — | GitHub username to assign during runs |
| `repo_path` | yes | — | Local path to a clone of the target repo |
| `test_command` | yes | — | Shell command to verify fixes (e.g. `pytest`) |
| `agent_timeout_minutes` | no | `30` | Max runtime for Claude Code |
| `poll_interval_seconds` | no | `300` | Seconds between poll cycles |
| `claim_timeout_minutes` | no | `45` | Minutes before a stale claim is released |
| `db_path` | no | `bugagent.db` | SQLite database location |
| `log_path` | no | `logs/agent.log` | Log file location |

See `config.example.yaml` for a fully annotated template.

## Usage

```bash
# Single poll cycle (cron-friendly)
bugagent run --once --config config.yaml

# Daemon mode
bugagent run --config config.yaml

# View recent run history
bugagent status --config config.yaml

# Filter by issue number or time window
bugagent status --issue 42 --since yesterday --config config.yaml

# Release a stuck claim manually
bugagent release --issue 42 --config config.yaml

# Update a config value
bugagent config set key value --config config.yaml
```

## GitHub labels

`setup` creates these labels in your repository:

| Label | Meaning |
|-------|---------|
| `agent-queue` | Issue is ready for the agent |
| `in-progress` | Agent is actively working |
| `needs-human` | Agent could not fix — human review required |
| `agent-fix-pr` | PR opened, awaiting review |
| `agent-fix-unverified` | PR opened but tests could not run |

## Escalation

The agent escalates to `needs-human` and posts a diagnostic comment when:

- Claude Code times out
- Claude Code reports it cannot fix the issue (`AGENT_CANNOT_FIX:` marker)
- Tests fail after the fix
- No file changes were made
- PR creation fails (a patch file is saved as fallback)

## Development

```bash
make test    # run the test suite
make lint    # run linters
```

## License

MIT
