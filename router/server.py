"""Router server: holds the single Slack socket-mode connection, fans events to per-user
shims over outbound WebSocket, proxies whitelisted Slack API calls back through the same WS."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import WSMsgType, web
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from . import protocol as P
from .audit import Audit
from .config import RouterConfig, UserEntry, UsersStore
from .queue import EventQueue


log = logging.getLogger("claude-slack-router")


class ShimConn:
    """One connected shim. Owns its WS and a send lock."""

    def __init__(self, ws: web.WebSocketResponse, user: UserEntry) -> None:
        self.ws = ws
        self.user = user
        self.send_lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        async with self.send_lock:
            await self.ws.send_str(P.encode(msg))


class Router:
    def __init__(self, cfg: RouterConfig) -> None:
        self.cfg = cfg
        self.users = UsersStore()
        self.audit = Audit()
        self.queue = EventQueue(cfg.server.queue_ttl_seconds, cfg.server.queue_max_per_user)

        self.slack = AsyncApp(token=cfg.slack.bot_token)
        self.web = AsyncWebClient(token=cfg.slack.bot_token)
        self._bot_user_id = ""
        self._bot_name = "claude"

        self._shims: dict[str, ShimConn] = {}       # slack_user_id → ShimConn
        self._dm_to_user: dict[str, str] = {}       # DM channel → slack_user_id (cache)

        self._register_slack_handlers()

    # ---------- Slack handlers ----------

    def _register_slack_handlers(self) -> None:
        self.slack.event("message")(self.on_slack_message)
        self.slack.event("app_mention")(self.on_slack_app_mention)
        self.slack.event("reaction_added")(self.on_slack_reaction)
        self.slack.command("/claude")(self.on_slack_command)

    async def on_slack_message(self, event: dict) -> None:
        # Only DMs carry session content under the strict-isolation model.
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id"):
            return
        channel = event.get("channel", "")
        user = await self._user_for_dm(channel)
        if not user:
            return
        await self._deliver(user.slack_user_id, event)

    async def on_slack_app_mention(self, event: dict) -> None:
        user_id = event.get("user", "")
        if not user_id or self.users.get(user_id) is None:
            return
        await self._deliver(user_id, event)

    async def on_slack_reaction(self, event: dict) -> None:
        user_id = event.get("user", "")
        if not user_id or self.users.get(user_id) is None:
            return
        await self._deliver(user_id, event)

    async def on_slack_command(self, ack, body, respond) -> None:
        """/claude register | revoke | status. Self-serve API key issuance."""
        await ack()
        user_id = body.get("user_id", "")
        user_name = body.get("user_name", "") or user_id
        text = (body.get("text") or "").strip().lower()
        sub = (text.split() or ["register"])[0]

        if sub == "register":
            await self._cmd_register(user_id, user_name, respond)
        elif sub == "revoke":
            await self._cmd_revoke(user_id, respond)
        elif sub == "status":
            await self._cmd_status(user_id, respond)
        else:
            await respond(text=":grey_question: Usage: `/claude register` | `/claude revoke` | `/claude status`")

    async def _cmd_register(self, user_id: str, user_name: str, respond) -> None:
        existed = self.users.get(user_id) is not None
        # Close any existing live connection so the old key can't be used after rotation.
        if existed:
            existing = self._shims.get(user_id)
            if existing is not None:
                try:
                    await existing.ws.close()
                except Exception:
                    pass
        key = self.users.add(user_id, user_name)
        self.audit.write("register", user=user_id, name=user_name, rotated=existed)

        url = self.cfg.server.public_url or f"ws://<router-host>:{self.cfg.server.port}/v1/connect"
        snippet = (
            ":key: *Your claude-slack API key*  "
            f"({'rotated — old key no longer works' if existed else 'new'})\n\n"
            "Paste this into `~/.config/claude-slack/config.toml`:\n"
            "```\n"
            "[router]\n"
            f"url = \"{url}\"\n"
            f"api_key = \"{key}\"\n"
            "```\n"
            ":warning: This is the only time you'll see this key. "
            "If you lose it, run `/claude register` again to rotate.\n\n"
            "Or, when `claude-slack mirror` prompts you, paste just the api_key value: "
            f"`{key}`"
        )
        try:
            dm = await self.web.conversations_open(users=user_id)
            ch = (dm.get("channel") or {}).get("id", "")
            if ch:
                await self.web.chat_postMessage(channel=ch, text=snippet)
                await respond(text=":mailbox_with_mail: I just DM'd you your API key.")
                return
        except Exception as e:
            log.warning("DM failed for %s: %s", user_id, e)
        # Fallback: ephemeral reply with the key (less private but recoverable).
        await respond(text=f"I couldn't DM you. Here's your key (ephemeral):\n{snippet}")

    async def _cmd_revoke(self, user_id: str, respond) -> None:
        if not self.users.revoke(user_id):
            await respond(text=":grey_question: No key on file for you. Nothing to revoke.")
            return
        existing = self._shims.get(user_id)
        if existing is not None:
            try:
                await existing.ws.close()
            except Exception:
                pass
        self.audit.write("revoke", user=user_id)
        await respond(text=":no_entry: revoked. Your mirror sessions are disconnected. "
                            "Run `/claude register` to get a new key.")

    async def _cmd_status(self, user_id: str, respond) -> None:
        entry = self.users.get(user_id)
        if entry is None:
            await respond(text=":grey_question: No key on file. Run `/claude register`.")
            return
        connected = user_id in self._shims
        age_days = (time.time() - entry.created_at) / 86400 if hasattr(entry, "created_at") else 0
        await respond(text=(
            f"*status*  registered: yes  ·  shim connected: {connected}  ·  "
            f"key age: {age_days:.1f}d  ·  queued events: {self.queue.depth(user_id)}"
        ))

    # ---------- routing ----------

    async def _deliver(self, slack_user_id: str, payload: dict) -> None:
        self.audit.write("event", user=slack_user_id, kind=payload.get("type", "?"))
        conn = self._shims.get(slack_user_id)
        if conn is None:
            self.queue.enqueue(slack_user_id, payload)
            return
        try:
            await conn.send(P.event(payload))
        except Exception as e:
            log.warning("send to %s failed: %s; queueing", slack_user_id, e)
            self.queue.enqueue(slack_user_id, payload)

    async def _user_for_dm(self, channel_id: str) -> UserEntry | None:
        if not channel_id:
            return None
        cached = self._dm_to_user.get(channel_id)
        if cached:
            return self.users.get(cached)
        try:
            info = await self.web.conversations_info(channel=channel_id)
            ch = info.get("channel") or {}
            if not ch.get("is_im"):
                return None
            counterparty = ch.get("user", "")
            if counterparty:
                self._dm_to_user[channel_id] = counterparty
            return self.users.get(counterparty)
        except Exception as e:
            log.debug("conversations_info(%s) failed: %s", channel_id, e)
            return None

    # ---------- WebSocket handler ----------

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        conn: ShimConn | None = None
        try:
            msg = await ws.receive(timeout=10)
            if msg.type != WSMsgType.TEXT:
                await ws.close(); return ws
            hello = P.decode(msg.data)
            if hello.get("type") != "hello":
                await ws.send_str(P.encode(P.auth_error("expected hello"))); await ws.close()
                return ws
            user = self.users.authenticate(hello.get("api_key", ""))
            if not user:
                await ws.send_str(P.encode(P.auth_error("invalid api key"))); await ws.close()
                self.audit.write("auth_fail", reason="invalid_key")
                return ws

            conn = ShimConn(ws, user)
            # New connection supersedes any older one.
            existing = self._shims.get(user.slack_user_id)
            if existing is not None:
                try:
                    await existing.ws.close()
                except Exception:
                    pass
            self._shims[user.slack_user_id] = conn
            await conn.send(P.welcome(
                slack_user_id=user.slack_user_id,
                bot_user_id=self._bot_user_id,
                bot_name=self._bot_name,
            ))
            self.audit.write("connect", user=user.slack_user_id, name=user.name)
            log.info("shim connected: %s (%s)", user.name, user.slack_user_id)

            # Drain offline queue (best-effort, oldest first).
            for payload in self.queue.drain(user.slack_user_id):
                try:
                    await conn.send(P.event(payload))
                except Exception:
                    break

            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    decoded = P.decode(raw.data)
                except Exception:
                    continue
                await self._handle_shim_msg(conn, decoded)
        finally:
            if conn is not None and self._shims.get(conn.user.slack_user_id) is conn:
                del self._shims[conn.user.slack_user_id]
                self.audit.write("disconnect", user=conn.user.slack_user_id)
                log.info("shim disconnected: %s", conn.user.name)
        return ws

    # ---------- shim → router messages ----------

    async def _handle_shim_msg(self, conn: ShimConn, msg: dict) -> None:
        t = msg.get("type", "")
        if t == "ping":
            await conn.send(P.pong())
        elif t == "api_call":
            await self._handle_api_call(conn, msg)
        elif t == "turn_complete":
            self.audit.write(
                "turn_complete",
                user=conn.user.slack_user_id,
                thread_ts=msg.get("thread_ts", ""),
                cost_usd=msg.get("cost_usd", 0.0),
                num_turns=msg.get("num_turns", 0),
            )

    async def _handle_api_call(self, conn: ShimConn, msg: dict) -> None:
        request_id = msg.get("request_id", "")
        method = msg.get("method", "")
        params = dict(msg.get("params") or {})

        if method not in P.ALLOWED_METHODS:
            await conn.send(P.api_response(request_id, False,
                                            error=f"method not allowed: {method}"))
            return

        ok, scope_err = await self._scope_check(conn.user, method, params)
        if not ok:
            await conn.send(P.api_response(request_id, False, error=scope_err))
            return

        # Normalize SDK alias `files_upload_v2` → `files.upload_v2` for api_call.
        slack_method = method.replace("_", ".") if method.startswith("files_") else method
        try:
            resp = await self.web.api_call(slack_method, params=params)
            data = resp.data if hasattr(resp, "data") else dict(resp)
            await conn.send(P.api_response(request_id, True, response=data))
        except Exception as e:
            await conn.send(P.api_response(request_id, False, error=str(e)))

    async def _scope_check(self, user: UserEntry, method: str, params: dict) -> tuple[bool, str]:
        ch = params.get("channel") or params.get("channel_id") or ""
        if not ch:
            return True, ""
        if ch.startswith("U"):
            if ch != user.slack_user_id:
                return False, f"scope: cannot target user {ch}"
            return True, ""
        if ch.startswith("D"):
            owner = await self._user_for_dm(ch)
            if owner is None or owner.slack_user_id != user.slack_user_id:
                return False, f"scope: DM {ch} not owned by this user"
            return True, ""
        # Public/private channel target: require membership.
        try:
            info = await self.web.conversations_members(channel=ch)
            members = info.get("members", []) or []
            if user.slack_user_id not in members:
                return False, f"scope: not a member of {ch}"
        except Exception as e:
            return False, f"scope check failed: {e}"
        return True, ""

    # ---------- lifecycle ----------

    async def start(self) -> None:
        try:
            me = await self.web.auth_test()
            self._bot_user_id = me.get("user_id", "")
            self._bot_name = me.get("user", "claude")
        except Exception as e:
            log.warning("auth_test failed: %s", e)

        sm_handler = AsyncSocketModeHandler(self.slack, self.cfg.slack.app_token)
        sm_task = asyncio.create_task(sm_handler.start_async())

        app = web.Application()
        app.router.add_get("/v1/connect", self.ws_handler)
        app.router.add_get("/healthz", lambda _r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.server.host, self.cfg.server.port)
        await site.start()
        log.info("router up on %s:%s (bot=@%s, %d users)",
                 self.cfg.server.host, self.cfg.server.port,
                 self._bot_name, len(self.users.list()))
        try:
            await sm_task
        except asyncio.CancelledError:
            await runner.cleanup()
            raise


def run() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .config import load_router, router_exists
    if not router_exists():
        print("Router not configured. Run: claude-slack-router init", flush=True)
        return 1
    cfg = load_router()
    if not cfg.slack.bot_token or not cfg.slack.app_token:
        print("Router config missing slack tokens. Run: claude-slack-router init", flush=True)
        return 1
    router = Router(cfg)
    try:
        asyncio.run(router.start())
        return 0
    except KeyboardInterrupt:
        return 130
