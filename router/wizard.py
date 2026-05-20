"""Router admin init wizard. One-time setup on the shared host."""
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

from claude_slack import clipboard
from . import config as cfg_mod
from .config import RouterConfig, SlackConfig, ServerConfig


console = Console()

MANIFEST_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-slack-router" / "manifest.json"


def _manifest(display_name: str = "claude") -> dict:
    return {
        "display_information": {
            "name": display_name,
            "description": "Mirror local Claude Code sessions into per-user Slack DMs",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": display_name, "always_online": True},
            "slash_commands": [{
                "command": "/claude",
                "description": "Register your local shim with the router",
                "usage_hint": "register | revoke | status",
                "should_escape": False,
            }],
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "chat:write",
                    "commands",
                    "files:read",
                    "files:write",
                    "im:history",
                    "im:read",
                    "im:write",
                    "reactions:read",
                    "reactions:write",
                    "users:read",
                ],
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": [
                    "app_mention",
                    "message.im",
                    "reaction_added",
                ],
            },
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def _rule(title: str) -> None:
    console.print(); console.print(Rule(f"[bold cyan]{title}[/bold cyan]", style="cyan"))


def _step(label: str, body: str) -> None:
    console.print(f"  [bold green]{label}[/bold green]  {body}")


async def _pause(prompt: str = "Press Enter when ready") -> None:
    await questionary.text(prompt, default="", instruction="(Enter)").ask_async()


async def _verify(bot_token: str) -> tuple[bool, str, str, str]:
    client = AsyncWebClient(token=bot_token)
    try:
        resp = await client.auth_test()
        return True, resp.get("team", "?"), resp.get("user", "?"), ""
    except Exception as e:
        return False, "", "", str(e)


async def _run_async() -> int:
    console.print(Panel.fit(
        "[bold cyan]claude-slack-router setup[/bold cyan]\n"
        "One-time admin install. Creates the shared Slack app, writes router config.\n"
        "Per-user onboarding happens with [bold]add-user[/bold] after this.",
        border_style="cyan",
    ))

    if cfg_mod.router_exists():
        if not await questionary.confirm(
            f"Router config exists at {cfg_mod.CONFIG_PATH}. Overwrite?", default=False,
        ).ask_async():
            console.print("[yellow]Aborted.[/yellow]")
            return 0

    name = await questionary.text(
        "Bot display name in Slack:", default="claude",
        validate=lambda v: (0 < len(v) <= 35) or "1-35 chars",
    ).ask_async()
    if not name:
        return 1

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest_text = json.dumps(_manifest(name), indent=2)
    MANIFEST_PATH.write_text(manifest_text)
    copied = clipboard.copy(manifest_text)

    _rule("Step 1 of 4 — Create the Slack app")
    if copied:
        console.print(Panel.fit(
            ":clipboard: [bold green]Manifest is in your clipboard.[/bold green]\n"
            f"   [dim]Also saved to[/dim] [bold]{MANIFEST_PATH}[/bold]",
            border_style="green",
        ))
    else:
        console.print(f"  [yellow]OSC 52 copy failed.[/yellow] Manifest at [bold]{MANIFEST_PATH}[/bold]")
    _step("1a.", "Open [link=https://api.slack.com/apps]https://api.slack.com/apps[/link]")
    _step("1b.", "Click [bold]Create New App[/bold] → [bold]From an app manifest[/bold]")
    _step("1c.", "Pick your workspace, click [bold]Next[/bold]")
    _step("1d.", "Click the [bold]JSON[/bold] tab, Cmd-A → Delete → paste → [bold]Next[/bold] → [bold]Create[/bold]")
    if not copied:
        console.print(Syntax(manifest_text, "json", word_wrap=False))
    await _pause("Press Enter once the app is created")

    _rule("Step 2 of 4 — Install + grab tokens")
    _step("2a.", "Left sidebar → [bold]Install App[/bold] → [bold]Install to Workspace[/bold] → [bold]Allow[/bold]")
    _step("2b.", "Left sidebar → [bold]OAuth & Permissions[/bold] → copy [bold]Bot User OAuth Token[/bold] (xoxb-...)")
    _step("2c.", "Left sidebar → [bold]Basic Information[/bold] → scroll to [bold]App-Level Tokens[/bold] → "
                  "[bold]Generate Token and Scopes[/bold], add [italic]connections:write[/italic], [bold]Generate[/bold] → copy (xapp-...)")
    bot_token = await questionary.password(
        "Paste Bot User OAuth Token (xoxb-...):",
        validate=lambda v: v.startswith("xoxb-") or "must start with xoxb-",
    ).ask_async()
    if not bot_token:
        return 1
    app_token = await questionary.password(
        "Paste App-Level Token (xapp-...):",
        validate=lambda v: v.startswith("xapp-") or "must start with xapp-",
    ).ask_async()
    if not app_token:
        return 1

    _rule("Step 3 of 4 — Verifying")
    ok, team, who, err = await _verify(bot_token)
    if not ok:
        console.print(f"[red]Auth failed:[/red] {err}")
        return 2
    console.print(f"  [green]OK[/green]  connected as [bold]{who}[/bold] in [bold]{team}[/bold]")

    _rule("Step 4 of 4 — Router server settings")
    host = await questionary.text("Bind host:", default="0.0.0.0").ask_async() or "0.0.0.0"
    port_s = await questionary.text("Bind port:", default="9000",
                                     validate=lambda v: v.isdigit() or "digits only").ask_async()
    port = int(port_s or 9000)
    public_url = await questionary.text(
        "Public URL teammates dial (wss://...):",
        default=f"ws://{host}:{port}/v1/connect",
    ).ask_async() or ""

    cfg = RouterConfig(
        slack=SlackConfig(bot_token=bot_token, app_token=app_token, workspace_name=team),
        server=ServerConfig(host=host, port=port, public_url=public_url),
    )
    cfg_mod.save_router(cfg)

    _rule("Done")
    console.print(f"  [green]Wrote[/green] {cfg_mod.CONFIG_PATH}")
    console.print()
    console.print(Panel.fit(
        f"[bold]Next:[/bold]\n"
        f"  1. Start the router: [bold cyan]claude-slack-router run[/bold cyan]\n"
        f"     [dim](use systemd / nohup / tmux to keep it up)[/dim]\n"
        f"  2. Onboard your first teammate:\n"
        f"     [bold cyan]claude-slack-router add-user --slack-user U... --name samay[/bold cyan]\n"
        f"     send them the printed snippet over Slack DM.",
        border_style="green",
    ))
    return 0


def run() -> int:
    try:
        return asyncio.run(_run_async())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 130
