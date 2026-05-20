"""Router config + users registry. Admin-managed; users authenticate by API key."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

import tomli_w


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "claude-slack-router"
CONFIG_PATH = CONFIG_DIR / "config.toml"
USERS_PATH = CONFIG_DIR / "users.toml"
AUDIT_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "claude-slack-router"
AUDIT_PATH = AUDIT_DIR / "audit.jsonl"


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    workspace_name: str = ""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 9000
    public_url: str = ""              # e.g. "wss://router.internal.example.com/v1/connect"
    queue_ttl_seconds: int = 86400    # 24h
    queue_max_per_user: int = 1000


@dataclass
class RouterConfig:
    slack: SlackConfig = field(default_factory=SlackConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def load_router(path: Path = CONFIG_PATH) -> RouterConfig:
    if not path.exists():
        return RouterConfig()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return RouterConfig(
        slack=SlackConfig(**raw.get("slack", {})),
        server=ServerConfig(**raw.get("server", {})),
    )


def save_router(cfg: RouterConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(asdict(cfg), f)
    path.chmod(0o600)


def router_exists(path: Path = CONFIG_PATH) -> bool:
    return path.exists()


# ---------- users registry ----------

@dataclass
class UserEntry:
    slack_user_id: str
    name: str
    api_key_hash: str
    created_at: float


class UsersStore:
    """users.toml maps slack_user_id → UserEntry. API keys hashed at rest."""

    def __init__(self, path: Path = USERS_PATH) -> None:
        self.path = path
        self._users: dict[str, UserEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("rb") as f:
            raw = tomllib.load(f)
        for slack_id, data in (raw.get("users") or {}).items():
            self._users[slack_id] = UserEntry(**data)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = {"users": {sid: asdict(u) for sid, u in self._users.items()}}
        with self.path.open("wb") as f:
            tomli_w.dump(out, f)
        self.path.chmod(0o600)

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def gen_api_key() -> str:
        return "cs_" + secrets.token_urlsafe(32)

    def add(self, slack_user_id: str, name: str) -> str:
        """Generate + store a new key. Returns plaintext key (shown to admin once)."""
        key = self.gen_api_key()
        self._users[slack_user_id] = UserEntry(
            slack_user_id=slack_user_id, name=name,
            api_key_hash=self._hash(key), created_at=time.time(),
        )
        self._flush()
        return key

    def revoke(self, slack_user_id: str) -> bool:
        if slack_user_id not in self._users:
            return False
        del self._users[slack_user_id]
        self._flush()
        return True

    def list(self) -> list[UserEntry]:
        return list(self._users.values())

    def authenticate(self, api_key: str) -> UserEntry | None:
        h = self._hash(api_key)
        for u in self._users.values():
            if hmac.compare_digest(u.api_key_hash, h):
                return u
        return None

    def get(self, slack_user_id: str) -> UserEntry | None:
        return self._users.get(slack_user_id)
