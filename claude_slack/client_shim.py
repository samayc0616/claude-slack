"""Shim variant that talks to a shared router instead of directly to Slack.

Reuses the PTY + IO machinery from shim.Shim but swaps the transport: a single
outbound WebSocket to the router carries both incoming events (Slack messages
that target this user) and outgoing Slack API calls (proxied through the router).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from contextlib import suppress
from typing import Any

import aiohttp

from . import slack_render as R
from .config import Config
from .redact import scrub
from .shim import Shim, _raw_terminal, _clean_for_slack, FLUSH_IDLE_SECONDS, FLUSH_MAX_BYTES, CHILD_REAP_POLL_SECONDS

# Reuse the protocol module from the router package so we never drift.
from router import protocol as P


log = logging.getLogger("claude-slack.client")


class ProxiedWebClient:
    """AsyncWebClient look-alike: every method routes through the router WS."""

    def __init__(self, send: Any, responses: dict[str, asyncio.Future]) -> None:
        self._send = send
        self._responses = responses

    async def _call(self, method: str, **kwargs) -> dict:
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._responses[request_id] = fut
        try:
            await self._send(P.api_call(request_id, method, kwargs))
            result = await asyncio.wait_for(fut, timeout=30)
        finally:
            self._responses.pop(request_id, None)
        if not result.get("ok"):
            raise RuntimeError(f"{method} failed: {result.get('error', '?')}")
        return result.get("response") or {}

    async def auth_test(self) -> dict:
        return await self._call("auth.test")

    async def conversations_open(self, users: str) -> dict:
        return await self._call("conversations.open", users=users)

    async def conversations_info(self, channel: str) -> dict:
        return await self._call("conversations.info", channel=channel)

    async def chat_postMessage(self, channel: str, text: str = "",
                                thread_ts: str = "", blocks: list | None = None) -> dict:
        kwargs: dict[str, Any] = {"channel": channel}
        if text:
            kwargs["text"] = text
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks is not None:
            kwargs["blocks"] = blocks
        return await self._call("chat.postMessage", **kwargs)

    async def chat_update(self, channel: str, ts: str, text: str = "",
                           blocks: list | None = None) -> dict:
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts}
        if text:
            kwargs["text"] = text
        if blocks is not None:
            kwargs["blocks"] = blocks
        return await self._call("chat.update", **kwargs)

    async def chat_getPermalink(self, channel: str, message_ts: str) -> dict:
        return await self._call("chat.getPermalink", channel=channel, message_ts=message_ts)

    async def files_upload_v2(self, channel: str = "", thread_ts: str = "",
                                filename: str = "", content: str = "",
                                initial_comment: str = "") -> dict:
        kwargs: dict[str, Any] = {}
        if channel:
            kwargs["channel"] = channel
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if filename:
            kwargs["filename"] = filename
        if content:
            kwargs["content"] = content
        if initial_comment:
            kwargs["initial_comment"] = initial_comment
        return await self._call("files_upload_v2", **kwargs)

    async def reactions_add(self, channel: str, timestamp: str, name: str) -> dict:
        return await self._call("reactions.add", channel=channel,
                                 timestamp=timestamp, name=name)


class ClientShim(Shim):
    """Shim that connects to a router. Replaces Slack-direct transport with WS proxy."""

    def __init__(self, cfg: Config, claude_args: list[str]) -> None:
        # Skip the parent's __init__ Slack-direct setup; rebuild minimally.
        self.cfg = cfg
        self.claude_args = claude_args

        self.master_fd: int = -1
        self.child_pid: int = -1
        self.exit_code: int = 0

        self._bot_user_id: str = ""
        self._bot_name: str = "claude"
        self._dm_channel: str = ""
        self._dm_user_id: str = ""
        self._thread_ts: str = ""

        self._out_buffer = bytearray()
        self._last_output_at = 0.0
        self._buffer_lock = asyncio.Lock()
        self._stop = asyncio.Event()

        # Transport bits.
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http: aiohttp.ClientSession | None = None
        self._send_lock = asyncio.Lock()
        self._responses: dict[str, asyncio.Future] = {}

        # ProxiedWebClient stands in for self.web.
        self.web = ProxiedWebClient(self._send, self._responses)

    # ---------- transport ----------

    async def _send(self, msg: dict) -> None:
        if self._ws is None or self._ws.closed:
            raise RuntimeError("router ws closed")
        async with self._send_lock:
            await self._ws.send_str(P.encode(msg))

    async def _connect_router(self) -> bool:
        url = self.cfg.router.url
        if not url:
            sys.stderr.write("claude-slack mirror: no [router] config\n")
            return False
        self._http = aiohttp.ClientSession()
        try:
            self._ws = await self._http.ws_connect(url, heartbeat=30, max_msg_size=8 * 1024 * 1024)
        except Exception as e:
            sys.stderr.write(f"claude-slack mirror: cannot reach router {url}: {e}\n")
            return False
        await self._ws.send_str(P.encode(P.hello(self.cfg.router.api_key, "0.1.0")))
        try:
            first = await asyncio.wait_for(self._ws.receive(), timeout=10)
        except asyncio.TimeoutError:
            sys.stderr.write("claude-slack mirror: router did not respond to hello\n")
            return False
        if first.type != aiohttp.WSMsgType.TEXT:
            sys.stderr.write("claude-slack mirror: router closed before welcome\n")
            return False
        frame = P.decode(first.data)
        if frame.get("type") == "auth_error":
            sys.stderr.write(f"claude-slack mirror: router rejected: {frame.get('reason')}\n")
            return False
        if frame.get("type") != "welcome":
            sys.stderr.write(f"claude-slack mirror: unexpected first frame {frame.get('type')}\n")
            return False
        self._bot_user_id = frame.get("bot_user_id", "")
        self._bot_name = frame.get("bot_name", "claude")
        self._dm_user_id = frame.get("slack_user_id", "")
        log.info("connected to router as @%s (bot=@%s)", self._dm_user_id, self._bot_name)
        return True

    async def _ws_reader(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = P.decode(msg.data)
                except Exception:
                    continue
                t = frame.get("type", "")
                if t == "event":
                    await self._dispatch_event(frame.get("payload") or {})
                elif t == "api_response":
                    fut = self._responses.get(frame.get("request_id", ""))
                    if fut and not fut.done():
                        fut.set_result(frame)
                elif t == "pong":
                    pass
        finally:
            self._stop.set()

    async def _dispatch_event(self, event: dict) -> None:
        et = event.get("type", "")
        if et == "message":
            await self._on_slack_message(event)
        elif et == "reaction_added":
            await self._on_slack_reaction(event)
        elif et == "app_mention":
            # Front-door redirect: post an ephemeral suggesting they DM the bot.
            ch = event.get("channel", "")
            user = event.get("user", "")
            try:
                await self.web._call(
                    "chat.postEphemeral",
                    channel=ch, user=user,
                    text=":inbox_tray: let's continue in our DM — I mirror your local "
                         "claude session there. (No session content lives in this channel.)",
                )
            except Exception as e:
                log.debug("app_mention ephemeral failed: %s", e)

    # ---------- override _bootstrap; parent's tries to call auth.test on its own web ----------

    async def _bootstrap(self) -> None:
        # bot identity already filled in from the welcome frame.
        pass

    # ---------- lifecycle override (no slack_bolt, no socket mode) ----------

    async def start(self) -> None:
        if not await self._connect_router():
            self.exit_code = 1
            return
        reader = asyncio.create_task(self._ws_reader())
        try:
            self._spawn_claude()
            with _raw_terminal(sys.stdin.fileno()):
                await self._io_loop()
        finally:
            reader.cancel()
            with suppress(Exception):
                if self._ws is not None:
                    await self._ws.close()
            with suppress(Exception):
                if self._http is not None:
                    await self._http.close()


def _bootstrap_config():
    """Resolve router URL + api_key. URL can come from env var or config; key is
    interactively prompted on first launch when missing."""
    from .config import load, save
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule

    console = Console()
    cfg = load()
    env_url = os.environ.get("CLAUDE_SLACK_ROUTER_URL", "").strip()
    if env_url and not cfg.router.url:
        cfg.router.url = env_url

    if not cfg.router.url:
        console.print(Panel.fit(
            "[bold red]claude-slack mirror: not configured.[/bold red]\n\n"
            "  Set the router URL:\n"
            "  [bold cyan]export CLAUDE_SLACK_ROUTER_URL=ws://<router-host>:31415/v1/connect[/bold cyan]\n\n"
            "  Then re-run [bold]claude-slack mirror[/bold].\n"
            "  Ask your admin for the router host if you don't know it.",
            border_style="red",
        ))
        return None

    if not cfg.router.api_key:
        console.print()
        console.print(Panel.fit(
            "[bold cyan]claude-slack mirror · first-time setup[/bold cyan]\n\n"
            "Welcome! This is a 30-second one-time setup. After this you just run\n"
            "[bold]claude-slack mirror[/bold] (or alias it to [bold]claude[/bold]) and forget about it.\n\n"
            f"[dim]Router:[/dim] [bold]{cfg.router.url}[/bold]",
            border_style="cyan",
        ))
        console.print()
        console.print(Rule("[bold cyan]Step 1 of 2[/bold cyan]  get your API key from Slack", style="cyan"))
        console.print()
        console.print("  In any Slack channel or DM, type:")
        console.print("    [bold green]/claude register[/bold green]")
        console.print()
        console.print("  The [bold]Claude Code Companion[/bold] bot will DM you a key that starts with"
                       " [bold]cs_…[/bold]")
        console.print("  Also: a pinned message in that DM explains how to use the bot.")
        console.print()
        console.print(Rule("[bold cyan]Step 2 of 2[/bold cyan]  paste the key here", style="cyan"))
        console.print()
        console.print("  [dim]Copy just the cs_… value (not the whole [router] block).[/dim]")
        console.print("  [dim]Your input will be hidden as you paste.[/dim]")
        console.print()
        try:
            import getpass
            key = getpass.getpass("  API key (cs_...): ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Aborted.[/yellow]")
            return None
        if not key.startswith("cs_"):
            console.print("\n[bold red]That doesn't look like an API key (should start with cs_).[/bold red]")
            console.print("[dim]Make sure you copied just the key, not the surrounding `api_key = \"…\"` text.[/dim]")
            return None
        cfg.router.api_key = key
        save(cfg)
        console.print()
        console.print(Panel.fit(
            "[bold green]Saved.[/bold green] Configuration is at "
            "[bold]~/.config/claude-slack/config.toml[/bold]\n\n"
            "[dim]Connecting to router and launching claude…[/dim]\n\n"
            "[dim]Tip: open your DM with the Claude Code Companion bot in Slack "
            "to watch this session mirror in real time.[/dim]",
            border_style="green",
        ))
        console.print()

    return cfg


def run(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    cfg = _bootstrap_config()
    if cfg is None:
        return 1
    sh = ClientShim(cfg, argv)
    try:
        asyncio.run(sh.start())
        return sh.exit_code
    except KeyboardInterrupt:
        return 130
