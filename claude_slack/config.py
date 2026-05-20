"""Config file at ~/.config/claude-slack/config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

import tomli_w


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "claude-slack"
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    workspace_name: str = ""
    slack_user_id: str = ""    # the installer's Slack user id; mirror DMs to this user


@dataclass
class ClaudeConfig:
    default_cwd: str = ""
    model: str = "claude-opus-4-7"


@dataclass
class FeaturesConfig:
    secret_redaction: bool = True


@dataclass
class RouterClientConfig:
    """Set when this shim talks to a shared router instead of directly to Slack."""
    url: str = ""
    api_key: str = ""


@dataclass
class Config:
    slack: SlackConfig = field(default_factory=SlackConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    router: RouterClientConfig = field(default_factory=RouterClientConfig)


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        slack=SlackConfig(**raw.get("slack", {})),
        claude=ClaudeConfig(**raw.get("claude", {})),
        features=FeaturesConfig(**raw.get("features", {})),
        router=RouterClientConfig(**raw.get("router", {})),
    )


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(asdict(cfg), f)
    path.chmod(0o600)


def exists(path: Path = CONFIG_PATH) -> bool:
    return path.exists()
