"""Solo-mode setup wizard. Creates a personal Slack app, picks the installer user,
writes config. The team-mode wizard lives in router/wizard.py."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from slack_sdk.web.async_client import AsyncWebClient

from . import clipboard
from . import config as cfg_mod
from .config import Config, SlackConfig, ClaudeConfig, FeaturesConfig

console = Console()

MANIFEST_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-slack" / "manifest.json"

DEFAULT_BOT_NAME = "claude"


def _manifest(display_name: str) -> dict:
    """Solo-mode manifest. Mirror-only, no slash commands or AI Apps surface."""
    return {
        "display_information": {
            "name": display_name,
            "description": "Mirror your local Claude Code session into a private Slack DM",
        },
        "features": {
            "bot_user": {"display_name": display_name, "always_online": True},
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "chat:write",
                    "files:read",
                    "files:write",
                    "im:history",
                    "im:read",
                    "im:write",
                    "pins:read",
                    "pins:write",
                    "reactions:read",
                    "users:read",
                ],
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": [
                    "message.im",
                    "reaction_added",
                ],
            },
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def _rule(title: str) -> None:
    console.print()
    console.print(Rule(f"[bold cyan]{title}[/bold cyan]", style="cyan"))


def _step(label: str, body: str) -> None:
    console.print(f"  [bold green]{label}[/bold green]  {body}")


async def _pause(prompt: str = "Press Enter when ready") -> None:
    await questionary.text(prompt, default="", instruction="(Enter)").ask_async()


# ---------- steps ----------

async def _step_bot_name() -> str:
    _rule("Step 1 of 5 — Bot name")
    console.print("  This is the @-name your bot shows up as in Slack. You'll DM this user.")
    return await questionary.text(
        "Bot display name:",
        default=DEFAULT_BOT_NAME,
        validate=lambda v: (0 < len(v) <= 35) or "1-35 chars",
    ).ask_async() or DEFAULT_BOT_NAME


async def _step_create_app(name: str) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_manifest(name), indent=2)
    MANIFEST_PATH.write_text(text)
    copied = clipboard.copy(text)

    _rule("Step 2 of 5 — Create the Slack app")
    if copied:
        console.print(Panel.fit(
            ":clipboard: [bold green]Manifest is on your clipboard.[/bold green]\n"
            f"   [dim]Also saved to[/dim] [bold]{MANIFEST_PATH}[/bold]",
            border_style="green",
        ))
    else:
        console.print(f"  [yellow]OSC 52 copy failed.[/yellow] Manifest at [bold]{MANIFEST_PATH}[/bold]")

    console.print()
    _step("2a.", "Open [link=https://api.slack.com/apps]https://api.slack.com/apps[/link]")
    _step("2b.", "Click [bold]Create New App[/bold] → [bold]From an app manifest[/bold]")
    _step("2c.", "Pick your workspace, click [bold]Next[/bold]")
    _step("2d.", "Click the [bold]JSON[/bold] tab → Cmd-A → Delete → paste → [bold]Next[/bold] → [bold]Create[/bold]")
    if not copied:
        console.print(Syntax(text, "json", word_wrap=False))
    await _pause("Press Enter once the app is created")


async def _step_install() -> None:
    _rule("Step 3 of 5 — Install the app + grab tokens")
    _step("3a.", "Left sidebar → [bold]Install App[/bold] → [bold]Install to Workspace[/bold] → [bold]Allow[/bold]")
    _step("3b.", "[bold]OAuth & Permissions[/bold] page → copy [bold]Bot User OAuth Token[/bold] (xoxb-...)")
    _step("3c.", "[bold]Basic Information[/bold] → scroll to [bold]App-Level Tokens[/bold] → "
                  "[bold]Generate Token and Scopes[/bold], add [italic]connections:write[/italic], "
                  "[bold]Generate[/bold] → copy (xapp-...)")


async def _ask_tokens() -> tuple[str, str]:
    bot = await questionary.password(
        "Bot User OAuth Token (xoxb-...):",
        validate=lambda v: v.startswith("xoxb-") or "must start with xoxb-",
    ).ask_async()
    if not bot:
        return "", ""
    app = await questionary.password(
        "App-Level Token (xapp-...):",
        validate=lambda v: v.startswith("xapp-") or "must start with xapp-",
    ).ask_async()
    return bot or "", app or ""


async def _verify(bot_token: str) -> tuple[bool, str, str, str]:
    client = AsyncWebClient(token=bot_token)
    try:
        resp = await client.auth_test()
        return True, resp.get("team", "?"), resp.get("user", "?"), ""
    except Exception as e:
        return False, "", "", str(e)


async def _list_human_users(bot_token: str) -> list[dict]:
    """Workspace members, filtered to humans (no bots, no deleted)."""
    client = AsyncWebClient(token=bot_token)
    out: list[dict] = []
    cursor = ""
    while True:
        resp = await client.users_list(cursor=cursor, limit=200)
        for u in resp.get("members", []) or []:
            if u.get("is_bot") or u.get("deleted") or u.get("id") == "USLACKBOT":
                continue
            out.append(u)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    return out


async def _step_pick_user(bot_token: str) -> str:
    _rule("Step 4 of 5 — Which Slack user is this for?")
    console.print("  Mirror DMs to one specific user (you). Pick yourself from the workspace.")
    console.print()
    users = await _list_human_users(bot_token)
    users.sort(key=lambda u: (u.get("profile", {}).get("display_name") or u.get("name") or u.get("id")))
    choices = [
        questionary.Choice(
            title=f"{(u.get('profile') or {}).get('real_name') or u.get('name', '?')}  ({u['id']})",
            value=u["id"],
        )
        for u in users
    ]
    if not choices:
        console.print("[red]No users visible.[/red] users:read scope missing?")
        return ""
    return await questionary.select(
        "Pick yourself:",
        choices=choices,
    ).ask_async() or ""


async def _step_locals() -> tuple[str, str, bool]:
    _rule("Step 5 of 5 — Local defaults")
    cwd = await questionary.path(
        "Default working directory:", default=str(Path.cwd()),
    ).ask_async() or str(Path.cwd())
    model = await questionary.select(
        "Model:",
        choices=["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        default="claude-opus-4-7",
    ).ask_async() or "claude-opus-4-7"
    redact = await questionary.confirm(
        "Redact secrets before mirroring to Slack?", default=True,
    ).ask_async()
    return cwd, model, bool(redact)


# ---------- main flow ----------

async def _run_async() -> int:
    console.print(Panel.fit(
        "[bold cyan]claude-slack setup (solo mode)[/bold cyan]\n"
        "Creates a personal Slack app and configures the mirror.\n"
        "[dim]For team deployments, use the router instead — see router/README.md.[/dim]",
        border_style="cyan",
    ))

    if cfg_mod.exists():
        if not await questionary.confirm(
            f"Config exists at {cfg_mod.CONFIG_PATH}. Overwrite?", default=False,
        ).ask_async():
            console.print("[yellow]Aborted.[/yellow]")
            return 0

    bot_name = await _step_bot_name()
    await _step_create_app(bot_name)
    await _step_install()
    bot_token, app_token = await _ask_tokens()
    if not bot_token or not app_token:
        return 1

    _rule("Verifying")
    ok, team, who, err = await _verify(bot_token)
    if not ok:
        console.print(f"[red]Auth failed:[/red] {err}")
        return 2
    console.print(f"  [green]OK[/green]  connected as [bold]{who}[/bold] in [bold]{team}[/bold]")

    slack_user_id = await _step_pick_user(bot_token)
    if not slack_user_id:
        return 3
    cwd, model, redact = await _step_locals()

    cfg = Config(
        slack=SlackConfig(
            bot_token=bot_token, app_token=app_token,
            workspace_name=team, slack_user_id=slack_user_id,
        ),
        claude=ClaudeConfig(default_cwd=cwd, model=model),
        features=FeaturesConfig(secret_redaction=redact),
    )
    cfg_mod.save(cfg)

    _rule("Done")
    console.print(f"  [green]Wrote[/green] {cfg_mod.CONFIG_PATH} (mode 600)")
    console.print()
    console.print(Panel.fit(
        f"[bold]Next:[/bold]\n"
        f"  Run [bold cyan]claude-slack mirror[/bold cyan] (or alias it to [bold]claude[/bold]).\n"
        f"  Open Slack and DM [bold]@{bot_name}[/bold] — that's where the mirror lands.",
        border_style="green",
    ))
    return 0


def run() -> int:
    try:
        return asyncio.run(_run_async())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 130
