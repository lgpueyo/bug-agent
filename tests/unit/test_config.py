"""Unit tests for bugagent.config"""
import pytest
import yaml
from pathlib import Path

from bugagent.config import load_config, Config, REQUIRED_KEYS, DEFAULTS


VALID_CONFIG = {
    "github_token": "ghp_testtoken123",
    "repo": "myorg/myrepo",
    "bot_username": "test-bot",
    "repo_path": "/tmp/repo",
    "test_command": "pytest",
}


def write_config(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "config.yaml"
    with open(p, "w") as f:
        yaml.dump(data, f)
    return str(p)


# ---------------------------------------------------------------------------
# test_load_config_valid
# ---------------------------------------------------------------------------

def test_load_config_valid(tmp_path):
    path = write_config(tmp_path, VALID_CONFIG)
    cfg = load_config(path)
    assert cfg.github_token == "ghp_testtoken123"
    assert cfg.repo == "myorg/myrepo"
    assert cfg.bot_username == "test-bot"
    assert cfg.repo_path == "/tmp/repo"
    assert cfg.test_command == "pytest"


# ---------------------------------------------------------------------------
# test_load_config_missing_required — one test per required field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing_key", REQUIRED_KEYS)
def test_load_config_missing_required(tmp_path, missing_key):
    data = {k: v for k, v in VALID_CONFIG.items() if k != missing_key}
    path = write_config(tmp_path, data)
    with pytest.raises(ValueError, match=missing_key):
        load_config(path)


# ---------------------------------------------------------------------------
# test_load_config_file_not_found
# ---------------------------------------------------------------------------

def test_load_config_file_not_found(tmp_path):
    missing = str(tmp_path / "does_not_exist.yaml")
    with pytest.raises(FileNotFoundError):
        load_config(missing)


# ---------------------------------------------------------------------------
# test_defaults_applied
# ---------------------------------------------------------------------------

def test_defaults_applied(tmp_path):
    path = write_config(tmp_path, VALID_CONFIG)
    cfg = load_config(path)
    assert cfg.agent_timeout_minutes == DEFAULTS["agent_timeout_minutes"]
    assert cfg.poll_interval_seconds == DEFAULTS["poll_interval_seconds"]
    assert cfg.claim_timeout_minutes == DEFAULTS["claim_timeout_minutes"]
    assert cfg.db_path == DEFAULTS["db_path"]
    assert cfg.log_path == DEFAULTS["log_path"]


def test_defaults_can_be_overridden(tmp_path):
    data = {**VALID_CONFIG, "agent_timeout_minutes": 10, "poll_interval_seconds": 60}
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.agent_timeout_minutes == 10
    assert cfg.poll_interval_seconds == 60


def test_agent_timeout_seconds_derived(tmp_path):
    data = {**VALID_CONFIG, "agent_timeout_minutes": 15}
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.agent_timeout_seconds == 900


def test_redacted_repr_hides_token(tmp_path):
    path = write_config(tmp_path, VALID_CONFIG)
    cfg = load_config(path)
    d = cfg.redacted_repr()
    assert d["github_token"] == "***REDACTED***"
    assert d["repo"] == "myorg/myrepo"


def test_empty_yaml_raises_value_error(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ValueError):
        load_config(str(p))


def test_anthropic_api_key_optional(tmp_path):
    """Config loads fine without anthropic_api_key."""
    path = write_config(tmp_path, VALID_CONFIG)
    cfg = load_config(path)
    assert not getattr(cfg, "anthropic_api_key", None)


def test_anthropic_api_key_loaded_when_present(tmp_path):
    data = {**VALID_CONFIG, "anthropic_api_key": "sk-ant-test123"}
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.anthropic_api_key == "sk-ant-test123"


def test_redacted_repr_hides_anthropic_key(tmp_path):
    data = {**VALID_CONFIG, "anthropic_api_key": "sk-ant-test123"}
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    d = cfg.redacted_repr()
    assert d["anthropic_api_key"] == "***REDACTED***"


def test_redacted_repr_anthropic_key_absent(tmp_path):
    """redacted_repr works when anthropic_api_key is not set."""
    path = write_config(tmp_path, VALID_CONFIG)
    cfg = load_config(path)
    d = cfg.redacted_repr()
    assert "anthropic_api_key" not in d or not d.get("anthropic_api_key")
