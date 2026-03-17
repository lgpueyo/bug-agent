import yaml
import os
from pathlib import Path

REQUIRED_KEYS = ["github_token", "repo", "bot_username", "repo_path", "test_command"]
DEFAULTS = {
    "agent_timeout_minutes": 30,
    "poll_interval_seconds": 300,
    "claim_timeout_minutes": 45,
    "db_path": "bugagent.db",
    "log_path": "logs/agent.log",
}

class Config:
    def __init__(self, data: dict):
        for key, val in data.items():
            setattr(self, key, val)
        self.agent_timeout_seconds = self.agent_timeout_minutes * 60

    def redacted_repr(self):
        d = {k: v for k, v in self.__dict__.items()}
        if "github_token" in d:
            d["github_token"] = "***REDACTED***"
        return d

def load_config(path: str = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(p) as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raw = {}
    for key in REQUIRED_KEYS:
        if not raw.get(key):
            raise ValueError(f"Missing required config key: '{key}' in {path}")
    merged = {**DEFAULTS, **raw}
    return Config(merged)
