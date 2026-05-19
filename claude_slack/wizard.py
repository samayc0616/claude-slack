"""TUI setup wizard. Walks the user through Slack app creation, scope-by-scope,
with pause points so each Slack-side click happens before the next instruction."""
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
from rich.text import Text
from slack_sdk.web.async_client import AsyncWebClient

from . import clipboard
from . import config as cfg_mod
from .config import Config, SlackConfig, ClaudeConfig, FeaturesConfig

console = Console()

MANIFEST_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-slack" / "manifest.json"

DEFAULT_BOT_NAME = "claude"


def _manifest(display_name: str) -> dict:
    return {
        "display_information": {
            "name": display_name,
            "description": "Drives local Claude Code sessions from Slack",
        },
        "features": {
            "bot_user": {"display_name": display_name, "always_online": True},
            "slash_commands": [{
                "command": "/claude",
                "description": "Control Claude Code sessions",
                "usage_hint": "new <prompt> | list | kill <thread_ts>",
                "should_escape": False,
            }],
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "channels:history",
                    "groups:history",
                    "im:history",
                    "mpim:history",
                    "chat:write",
                    "files:write",
                    "files:read",
                    "reactions:read",
                    "reactions:write",
                    "commands",
                    "users:read",
                ],
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": [
                    "app_mention",
                    "message.channels",
                    "message.groups",
                    "message.im",
                    "message.mpim",
                    "reaction_added",
                ],
            },
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


# ---------- presentation helpers ----------

def _banner() -> None:
    console.print(Panel.fit(
        "[bold cyan]claude-slack setup[/bold cyan]\n"
        "We'll create a Slack app, hook it up, and write your config.\n"
        "Total time: about 5 minutes.\n"
        "[dim]Tip: keep this terminal next to your browser so you can flip between them.[/dim]",
        border_style="cyan",
    ))


def _rule(title: str) -> None:
    console.print()
    console.print(Rule(f"[bold cyan]{title}[/bold cyan]", style="cyan"))


def _say(text: str) -> None:
    console.print(text)


def _step(label: str, body: str) -> None:
    console.print(f"  [bold green]{label}[/bold green]  {body}")


async def _pause(prompt: str = "Press Enter when ready") -> None:
    # questionary.press_any_key_to_continue is convenient but it eats characters;
    # plain Enter is friendlier.
    await questionary.text(prompt, default="", instruction="(Enter)").ask_async()


# ---------- the steps ----------

async def _step_bot_name() -> str:
    _rule("Step 1 of 6 — Pick a bot name")
    _say("This is what shows up in Slack. You'll @mention it to start sessions.")
    name = await questionary.text(
        "Bot display name:",
        default=DEFAULT_BOT_NAME,
        validate=lambda v: (len(v) > 0 and len(v) <= 35) or "1-35 chars",
    ).ask_async()
    return name or DEFAULT_BOT_NAME


def _write_manifest(name: str) -> Path:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(_manifest(name), indent=2))
    return MANIFEST_PATH


async def _step_create_app(name: str) -> None:
    path = _write_manifest(name)
    manifest_text = path.read_text()
    copied = clipboard.copy(manifest_text)

    _rule("Step 2 of 6 — Create the Slack app")

    if copied:
        console.print(Panel.fit(
            ":clipboard: [bold green]Manifest is in your clipboard.[/bold green]\n"
            f"   [dim]Also saved to[/dim] [bold]{path}[/bold]\n"
            "   [dim]If clipboard didn't take (some terminals block OSC 52),\n"
            "   re-copy from the file with[/dim] [bold]xclip -sel clip <[/bold] or just paste from the path.",
            border_style="green",
        ))
    else:
        console.print(Panel.fit(
            f":clipboard: [yellow]Could not OSC 52 copy.[/yellow] Manifest saved to:\n   [bold]{path}[/bold]\n"
            "   [dim]Copy it manually with[/dim] [bold]cat ~/.cache/claude-slack/manifest.json[/bold]\n"
            "   [dim]then select-all in your terminal.[/dim]",
            border_style="yellow",
        ))

    _say("")
    _step("2a.", "Open [link=https://api.slack.com/apps]https://api.slack.com/apps[/link] in a browser.")
    _step("2b.", "Sign in if needed. Click the green [bold]Create New App[/bold] button (top right).")
    _step("2c.", "A modal pops up. Click [bold]From an app manifest[/bold]"
                 " ([dim]not[/dim] [italic]From scratch[/italic]).")
    _step("2d.", "Pick your workspace from the dropdown, then click [bold]Next[/bold].")
    _step("2e.", "You'll land on a page titled [italic]Enter app manifest below[/italic].\n"
                 "         At the top of the code editor there are tabs: [bold]YAML[/bold] and [bold]JSON[/bold].\n"
                 "         Click the [bold]JSON[/bold] tab.")
    _step("2f.", "Click inside the editor. Select everything in it:"
                 " [bold]Cmd-A[/bold] on Mac / [bold]Ctrl-A[/bold] on Linux/Windows. Hit [bold]Delete[/bold].")
    _step("2g.", "Paste: [bold]Cmd-V[/bold] / [bold]Ctrl-V[/bold]."
                 " The manifest is already on your clipboard.")
    _step("2h.", "Click [bold]Next[/bold] at the bottom-right of the page.")
    _step("2i.", "Slack shows a summary of OAuth scopes, bot events, and the [italic]/claude[/italic] "
                 "slash command. Click [bold]Create[/bold].")

    if not copied:
        _say("\n[dim]Manifest contents (the JSON file you'll paste):[/dim]")
        console.print(Syntax(manifest_text, "json", word_wrap=False, line_numbers=False))

    await _pause("Press Enter once you see the new app's settings page in your browser")


async def _step_install_app() -> None:
    _rule("Step 3 of 6 — Install the app to your workspace")
    _step("3a.", "You're now on the app's settings page. The URL looks like"
                 " [italic]api.slack.com/apps/A0XXXXXX[/italic].")
    _step("3b.", "Look at the [bold]left sidebar[/bold]. Under the [italic]Settings[/italic] section,"
                 " click [bold]Install App[/bold].")
    _step("3c.", "You'll see a big [bold]Install to <Your Workspace>[/bold] button. Click it.")
    _step("3d.", "Slack shows an OAuth screen with the scopes the app wants. Click [bold]Allow[/bold].")
    _step("3e.", "You'll be returned to the [italic]Install App[/italic] page,"
                 " now showing a [bold]Bot User OAuth Token[/bold] at the top. Leave this tab open.")
    _say("\n[dim]Locked-down workspace? The [bold]Install to Workspace[/bold] button may say"
         " [italic]Request to Install[/italic] instead. You'll need an admin to approve before continuing.[/dim]")
    await _pause()


async def _step_bot_token() -> str:
    _rule("Step 4 of 6 — Copy the Bot User OAuth Token")
    _step("4a.", "In the [bold]left sidebar[/bold], under [italic]Features[/italic],"
                 " click [bold]OAuth & Permissions[/bold].")
    _step("4b.", "At the top of the page you'll see a [italic]Tokens for Your Workspace[/italic]"
                 " heading with [bold]Bot User OAuth Token[/bold] right below.")
    _step("4c.", "Click the [bold]Copy[/bold] button next to it."
                 " It starts with [italic]xoxb-[/italic] and is ~50 chars long.")
    _say("\n[dim]Token won't echo as you paste below.[/dim]\n")
    tok = await questionary.password(
        "Paste Bot User OAuth Token (xoxb-...):",
        validate=lambda v: v.startswith("xoxb-") or "must start with xoxb-",
    ).ask_async()
    return tok or ""


async def _step_app_token() -> str:
    _rule("Step 5 of 6 — Create an App-Level Token (for Socket Mode)")
    _step("5a.", "In the [bold]left sidebar[/bold], under [italic]Settings[/italic],"
                 " click [bold]Basic Information[/bold].")
    _step("5b.", "Scroll down the page until you find the [bold]App-Level Tokens[/bold] section"
                 " ([italic]below[/italic] [italic]Display Information[/italic]).")
    _step("5c.", "Click [bold]Generate Token and Scopes[/bold].")
    _step("5d.", "In the modal: [bold]Token Name[/bold] = [italic]socket-mode[/italic]"
                 " (or anything; just a label).")
    _step("5e.", "Click [bold]Add Scope[/bold], pick [bold]connections:write[/bold] from the dropdown.")
    _step("5f.", "Click the [bold]Generate[/bold] button. A new screen shows the token.")
    _step("5g.", "Click [bold]Copy[/bold]. It starts with [italic]xapp-[/italic]"
                 " and is significantly longer than the bot token.")
    _step("5h.", "Click [bold]Done[/bold] to close the modal.")
    _say("")
    tok = await questionary.password(
        "Paste App-Level Token (xapp-...):",
        validate=lambda v: v.startswith("xapp-") or "must start with xapp-",
    ).ask_async()
    return tok or ""


async def _verify(bot_token: str) -> tuple[bool, str, str, str]:
    """Returns (ok, team, user, error_or_empty)."""
    client = AsyncWebClient(token=bot_token)
    try:
        resp = await client.auth_test()
        return True, resp.get("team", "?"), resp.get("user", "?"), ""
    except Exception as e:
        return False, "", "", str(e)


async def _list_channels(bot_token: str) -> list[dict]:
    client = AsyncWebClient(token=bot_token)
    chans: list[dict] = []
    cursor = ""
    while True:
        resp = await client.conversations_list(
            cursor=cursor, limit=200,
            types="public_channel,private_channel,im,mpim",
            exclude_archived=True,
        )
        chans.extend(resp.get("channels", []))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    return chans


def _channel_label(c: dict) -> str:
    if c.get("is_im"):
        return f"DM with {c.get('user', '?')}"
    name = c.get("name") or c.get("id")
    kind = "private" if c.get("is_private") else "public"
    member = "" if c.get("is_member") else "  [not joined yet]"
    return f"#{name} ({kind}){member}"


async def _step_pick_channel(bot_token: str, bot_name: str) -> str:
    _rule("Step 6 of 6 — Choose a channel and invite the bot")
    _say(f"Before listing channels, invite the bot in Slack. In any channel where you "
         f"want it to live, type:  [bold]/invite @{bot_name}[/bold]")
    _say("[dim]You can do this in more than one channel; the bot can run in all of them. "
         "We just need a default for slash commands.[/dim]\n")
    await _pause("Press Enter after you've invited the bot")

    chans = await _list_channels(bot_token)
    chans.sort(key=lambda c: (not c.get("is_member", False),
                              c.get("is_im", False),
                              c.get("name") or ""))
    choices = [questionary.Choice(title=_channel_label(c), value=c["id"]) for c in chans]
    if not choices:
        console.print("[red]No channels visible to the bot.[/red] Invite it to one and re-run.")
        return ""
    return await questionary.select(
        "Default channel for slash commands:",
        choices=choices,
    ).ask_async() or ""


async def _step_defaults() -> tuple[str, str]:
    _rule("Defaults for new sessions")
    cwd = await questionary.path(
        "Default working directory for new sessions:",
        default=str(Path.cwd()),
    ).ask_async()
    model = await questionary.select(
        "Model:",
        choices=["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        default="claude-opus-4-7",
    ).ask_async()
    return cwd or str(Path.cwd()), model or "claude-opus-4-7"


async def _step_features() -> tuple[bool, bool, bool, bool]:
    _rule("Optional features")
    auto_name = await questionary.confirm("Auto-name threads from first prompt?", default=True).ask_async()
    redact = await questionary.confirm("Redact secrets before posting to Slack?", default=True).ask_async()
    card = await questionary.confirm("Pin a live session card at thread top?", default=True).ask_async()
    yolo = await questionary.confirm(
        "Pre-approve all tool calls (YOLO permissions)?", default=True,
    ).ask_async()
    return auto_name, redact, card, yolo


# ---------- main flow ----------

async def _run_async() -> int:
    _banner()

    if cfg_mod.exists():
        overwrite = await questionary.confirm(
            f"Config exists at {cfg_mod.CONFIG_PATH}. Overwrite?", default=False,
        ).ask_async()
        if not overwrite:
            console.print("[yellow]Aborted.[/yellow] Existing config left untouched.")
            return 0

    bot_name = await _step_bot_name()
    await _step_create_app(bot_name)
    await _step_install_app()
    bot_token = await _step_bot_token()
    if not bot_token:
        return 1
    app_token = await _step_app_token()
    if not app_token:
        return 1

    _rule("Verifying Slack connection")
    ok, team, user, err = await _verify(bot_token)
    if not ok:
        console.print(f"[red]Auth failed:[/red] {err}\n"
                      "  Common causes: wrong token, app not installed yet, "
                      "or you copied the Configuration Token instead of the Bot Token.")
        return 2
    console.print(f"  [green]OK[/green]  connected as [bold]{user}[/bold] "
                  f"in workspace [bold]{team}[/bold]")

    channel_id = await _step_pick_channel(bot_token, bot_name)
    if not channel_id:
        return 3

    default_cwd, model = await _step_defaults()
    auto_name, redact, card, yolo = await _step_features()

    config = Config(
        slack=SlackConfig(
            bot_token=bot_token, app_token=app_token,
            channel_id=channel_id, workspace_name=team,
        ),
        claude=ClaudeConfig(default_cwd=default_cwd, model=model),
        features=FeaturesConfig(
            auto_name_threads=auto_name, secret_redaction=redact,
            session_card=card, yolo_permissions=yolo,
        ),
    )
    cfg_mod.save(config)

    _rule("Done")
    console.print(f"  [green]Wrote[/green] {cfg_mod.CONFIG_PATH} (mode 600)")
    console.print(f"  [dim]Manifest cached at[/dim] {MANIFEST_PATH}")
    console.print()
    console.print(Panel.fit(
        f"[bold]Next:[/bold]\n"
        f"  1. Start the daemon: [bold cyan]uv run claude-slack run[/bold cyan]\n"
        f"     [dim](keep it in a tmux pane so you can watch logs)[/dim]\n"
        f"  2. In Slack, in a channel where you invited [bold]@{bot_name}[/bold], try:\n"
        f"     [bold]@{bot_name} hello[/bold]\n"
        f"  3. Reply in the thread it opens to continue the session.",
        border_style="green",
    ))
    return 0


def run() -> int:
    try:
        return asyncio.run(_run_async())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 130
