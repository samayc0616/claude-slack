"""Slack daemon. Socket-mode app that maps threads to Claude Code sessions."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from . import slack_render as R
from . import interactive as I
from .claude_proc import ClaudeSession, StreamEvent
from .config import Config, load
from .redact import scrub
from .sessions import Session, SessionManager

log = logging.getLogger("claude-slack")

INBOUND_FILES_DIR = Path("/tmp/claude-slack")


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.app = AsyncApp(token=cfg.slack.bot_token)
        self.web = AsyncWebClient(token=cfg.slack.bot_token)
        self.sessions = SessionManager()
        self._claude: dict[str, ClaudeSession] = {}  # thread_ts → live SDK client
        self._last_prompt: dict[str, str] = {}       # thread_ts → last user prompt
        self._pending_answers: dict[str, asyncio.Future[str]] = {}  # tool_use_id → Future
        self._pending_questions: dict[str, dict] = {}  # tool_use_id → AUQ args
        self._auq_selections: dict[str, dict[int, list[str]]] = {}  # tool_use_id → {q_idx: [choices]}
        self._injections: dict[str, list[str]] = {}  # thread_ts → queued user msgs while busy
        self._register_handlers()

    # ------- event registration -------

    def _register_handlers(self) -> None:
        self.app.event("app_mention")(self.on_mention)
        self.app.event("message")(self.on_message)
        self.app.event("reaction_added")(self.on_reaction)
        self.app.command("/claude")(self.on_slash)

        # Buttons / interactive components
        self.app.action("btn:interrupt")(self.on_interrupt_btn)
        self.app.action("btn:resend")(self.on_resend_btn)
        self.app.action(re.compile(r"^auq:[^:]+:\d+$"))(self.on_auq_select)
        self.app.action(re.compile(r"^auq_submit:.+$"))(self.on_auq_submit)
        self.app.action(re.compile(r"^auq_cancel:.+$"))(self.on_auq_cancel)
        self.app.action(re.compile(r"^plan_approve:.+$"))(self.on_plan_approve)
        self.app.action(re.compile(r"^plan_reject:.+$"))(self.on_plan_reject)

    # ------- event handlers -------

    async def on_mention(self, event: dict, say) -> None:
        text = _strip_mentions(event.get("text", ""))
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        paths = await self._stage_files(event, thread_ts)
        if paths:
            text += "\n\nFiles attached:\n" + "\n".join(f"- {p}" for p in paths)
        await self._dispatch(channel, thread_ts, text, parent_ts=event["ts"], user_id=user_id)

    async def on_message(self, event: dict, say) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        if not self.sessions.get(thread_ts):
            return
        text = event.get("text", "")
        user_id = event.get("user", "")
        paths = await self._stage_files(event, thread_ts)
        if paths:
            text += "\n\nFiles attached:\n" + "\n".join(f"- {p}" for p in paths)
        await self._dispatch(event["channel"], thread_ts, text, parent_ts=thread_ts, user_id=user_id)

    async def _dispatch(self, channel: str, thread_ts: str, text: str,
                        parent_ts: str, user_id: str) -> None:
        """Route an incoming user message: queue if busy, otherwise process now."""
        text = text.strip()
        if not text:
            return
        sess = self.sessions.get(thread_ts)
        if sess and sess.status in ("running", "waiting"):
            # Busy. Queue for the follow-up turn so the user feels heard immediately.
            self._injections.setdefault(thread_ts, []).append(text)
            try:
                await self.web.reactions_add(
                    channel=channel, timestamp=parent_ts, name="eyes",
                )
            except Exception:
                pass
            await self._say(
                channel, thread_ts,
                f"_:inbox_tray: queued for the next turn ({len(self._injections[thread_ts])} pending)_",
            )
            if sess and user_id:
                sess.last_user_id = user_id
                await self.sessions.upsert(sess)
            return
        await self._handle_turn(channel, thread_ts, text, parent_ts=parent_ts, user_id=user_id)

    async def on_reaction(self, event: dict) -> None:
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        ts = item.get("ts")
        channel = item.get("channel")
        name = event.get("reaction", "")

        # Map reaction → action. Reactor must be a human.
        sess = self._find_session_by_ts(ts) or self._find_session_by_thread(ts)
        if not sess:
            return
        if name == "no_entry":
            client = self._claude.get(sess.thread_ts)
            if client:
                await client.interrupt()
            await self.sessions.set_status(sess.thread_ts, "killed")
            await self._react(channel, sess.thread_ts, "killed")
        elif name == "repeat":
            last = self._last_prompt.get(sess.thread_ts, "")
            if last:
                await self._handle_turn(sess.channel, sess.thread_ts, last, parent_ts=sess.thread_ts)

    async def on_slash(self, ack, body, respond) -> None:
        await ack()
        text = (body.get("text") or "").strip()
        channel = body.get("channel_id") or self.cfg.slack.channel_id
        user = body.get("user_id", "")
        parts = text.split(maxsplit=1)
        cmd = parts[0] if parts else "list"
        rest = parts[1] if len(parts) > 1 else ""

        if cmd == "new":
            if not rest:
                await respond("Usage: /claude new <prompt>")
                return
            resp = await self.web.chat_postMessage(channel=channel, text=f"<@{user}> {rest}")
            thread_ts = resp["ts"]
            await self._handle_turn(channel, thread_ts, rest, parent_ts=thread_ts)
        elif cmd == "list":
            rows = self.sessions.all()
            if not rows:
                await respond("(no sessions)")
                return
            lines = [
                f"`{s.thread_ts}` {s.status:<8} ${s.total_cost_usd:.4f} `{s.cwd}`"
                + (f"  _{s.label}_" if s.label else "")
                for s in rows
            ]
            await respond("\n".join(lines))
        elif cmd == "kill":
            ts = rest.strip()
            client = self._claude.pop(ts, None)
            if client:
                await client.interrupt()
                await client.__aexit__(None, None, None)
            await self.sessions.remove(ts)
            await respond(f"killed `{ts}`")
        elif cmd == "cd":
            await respond("cd: change cwd by starting a fresh session in the target dir via `/claude new`")
        else:
            await respond("Usage: /claude new <prompt> | list | kill <thread_ts>")

    # ------- interactive button handlers -------

    async def on_interrupt_btn(self, ack, body) -> None:
        await ack()
        thread_ts = ((body.get("message") or {}).get("thread_ts")
                     or (body.get("container") or {}).get("thread_ts"))
        if not thread_ts:
            return
        client = self._claude.get(thread_ts)
        if client:
            await client.interrupt()
            await self.sessions.set_status(thread_ts, "killed")

    async def on_resend_btn(self, ack, body) -> None:
        await ack()
        thread_ts = ((body.get("message") or {}).get("thread_ts")
                     or (body.get("container") or {}).get("thread_ts"))
        if not thread_ts:
            return
        last = self._last_prompt.get(thread_ts, "")
        sess = self.sessions.get(thread_ts)
        if not last or not sess:
            return
        await self._handle_turn(sess.channel, thread_ts, last, parent_ts=thread_ts)

    async def on_auq_select(self, ack, body) -> None:
        await ack()
        action = body["actions"][0]
        action_id = action["action_id"]
        _, tool_use_id, q_idx_str = action_id.split(":", 2)
        q_idx = int(q_idx_str)
        if action["type"] == "checkboxes":
            chosen = [o["value"] for o in action.get("selected_options", []) or []]
        elif action["type"] == "radio_buttons":
            opt = action.get("selected_option") or {}
            chosen = [opt["value"]] if opt else []
        else:
            return
        self._auq_selections.setdefault(tool_use_id, {})[q_idx] = chosen

    async def on_auq_submit(self, ack, body) -> None:
        await ack()
        tool_use_id = body["actions"][0]["action_id"].split(":", 1)[1]
        questions = (self._pending_questions.get(tool_use_id) or {}).get("questions") or []
        selections = self._auq_selections.get(tool_use_id) or {}
        answers: dict[str, list[str] | str] = {}
        for i, q in enumerate(questions):
            picks = selections.get(i, [])
            header = q.get("header", f"q{i}")
            if q.get("multiSelect"):
                answers[header] = picks
            else:
                answers[header] = picks[0] if picks else ""
        msg = f"User answered via Slack: {json.dumps(answers)}"
        self._resolve(tool_use_id, msg)

    async def on_auq_cancel(self, ack, body) -> None:
        await ack()
        tool_use_id = body["actions"][0]["action_id"].split(":", 1)[1]
        self._resolve(tool_use_id, "User cancelled the question in Slack.")

    async def on_plan_approve(self, ack, body) -> None:
        await ack()
        tool_use_id = body["actions"][0]["action_id"].split(":", 1)[1]
        self._resolve(tool_use_id, "__APPROVE__")

    async def on_plan_reject(self, ack, body) -> None:
        await ack()
        tool_use_id = body["actions"][0]["action_id"].split(":", 1)[1]
        self._resolve(tool_use_id, "User rejected the plan in Slack.")

    def _resolve(self, tool_use_id: str, answer: str) -> None:
        fut = self._pending_answers.pop(tool_use_id, None)
        self._pending_questions.pop(tool_use_id, None)
        self._auq_selections.pop(tool_use_id, None)
        if fut and not fut.done():
            fut.set_result(answer)

    # ------- core turn handling -------

    async def _handle_turn(self, channel: str, thread_ts: str, prompt: str,
                            parent_ts: str, user_id: str = "") -> None:
        prompt = prompt.strip()
        if not prompt:
            return
        sess = self.sessions.get(thread_ts)
        is_new = sess is None
        if is_new:
            sess = Session(thread_ts=thread_ts, channel=channel, cwd=self.cfg.claude.default_cwd)
            sess.label = _autolabel(prompt) if self.cfg.features.auto_name_threads else ""
            if user_id:
                sess.last_user_id = user_id
            await self.sessions.upsert(sess)
            await self._post_card(sess)
            if self.cfg.features.auto_name_threads:
                asyncio.create_task(self._autoname_llm(sess.thread_ts, prompt))
        elif user_id:
            sess.last_user_id = user_id
            await self.sessions.upsert(sess)

        # Drain any queued @-mentions into this turn's prompt.
        queued = self._injections.pop(thread_ts, [])
        if queued:
            prompt = prompt + "\n\nAdditional context queued while you were working:\n" + \
                     "\n".join(f"- {m}" for m in queued)
        self._last_prompt[thread_ts] = prompt

        async with self.sessions.lock(thread_ts):
            await self.sessions.set_status(thread_ts, "running")
            await self._react(channel, parent_ts, "running")
            try:
                await self._run(sess, prompt)
                # If new messages arrived while running, immediately run a follow-up turn
                # so the user doesn't have to re-mention.
                follow_up = self._injections.pop(thread_ts, [])
                if follow_up:
                    follow_prompt = "Additional context from Slack:\n" + \
                                    "\n".join(f"- {m}" for m in follow_up)
                    await self._run(sess, follow_prompt)
                await self.sessions.set_status(thread_ts, "done")
                await self._react(channel, parent_ts, "done")
            except Exception as e:
                log.exception("session %s failed", thread_ts)
                await self._say(channel, thread_ts, f":x: *bridge error*\n```{e}```")
                await self.sessions.set_status(thread_ts, "error")
                await self._react(channel, parent_ts, "error")

    async def _run(self, sess: Session, prompt: str) -> None:
        client = self._claude.get(sess.thread_ts)
        if client is None:
            permission_mode = "bypassPermissions" if self.cfg.features.yolo_permissions else "default"
            client = ClaudeSession(
                cwd=sess.cwd or self.cfg.claude.default_cwd,
                model=self.cfg.claude.model,
                permission_mode=permission_mode,
                resume=sess.session_id,
                interactive_handler=lambda name, tid, args: self._interactive(sess, name, tid, args),
            )
            await client.__aenter__()
            self._claude[sess.thread_ts] = client

        async for ev in client.send(prompt):
            await self._render(sess, ev)

        sess.session_id = client.session_id or sess.session_id
        sess.total_cost_usd = client.total_cost_usd
        sess.total_tokens = client.total_tokens
        await self.sessions.upsert(sess)
        await self._update_card(sess)

    async def _render(self, sess: Session, ev: StreamEvent) -> None:
        ch, ts = sess.channel, sess.thread_ts
        if ev.kind == "text":
            text = ev.text
            if self.cfg.features.secret_redaction:
                text = scrub(text)
            text = R.md_to_mrkdwn(text)
            if R.is_long(text):
                await self._upload(ch, ts, text, filename="claude-output.md")
            else:
                for piece in R.chunk(text):
                    await self._say(ch, ts, piece)
        elif ev.kind == "tool_use":
            if not I.is_interactive(ev.tool_name):
                await self._say(ch, ts, f"_:wrench: {ev.tool_name}_")

    async def _interactive(self, sess: Session, tool_name: str,
                            tool_use_id: str, args: dict) -> str:
        """Called from inside ClaudeSession.can_use_tool. Posts UI to Slack, waits."""
        ch, ts = sess.channel, sess.thread_ts
        await self.sessions.set_status(sess.thread_ts, "waiting")
        await self._react(ch, ts, "waiting")

        if tool_name == "AskUserQuestion":
            blocks, fallback = I.render_ask_user_question(tool_use_id, args)
        elif tool_name == "ExitPlanMode":
            blocks, fallback = I.render_exit_plan_mode(tool_use_id, args)
        elif tool_name == "SendUserFile":
            # Just upload each file. No round-trip needed; allow tool to proceed.
            for path in args.get("files") or []:
                try:
                    await self.web.files_upload_v2(
                        channel=ch, thread_ts=ts,
                        file=path,
                        initial_comment=args.get("caption", ""),
                    )
                except Exception as e:
                    await self._say(ch, ts, f":warning: file upload failed for `{path}`: {e}")
            return "Files uploaded to Slack."
        else:
            return f"Unknown interactive tool: {tool_name}"

        self._pending_questions[tool_use_id] = args
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending_answers[tool_use_id] = fut
        await self.web.chat_postMessage(channel=ch, thread_ts=ts, text=fallback, blocks=blocks)
        # Fire-and-forget DM ping so the user sees a phone notification.
        asyncio.create_task(self._dm_waiting(sess, tool_name))
        try:
            answer = await asyncio.wait_for(fut, timeout=24 * 3600)
        except asyncio.TimeoutError:
            answer = "User did not answer within 24h; assume default."
        finally:
            self._pending_answers.pop(tool_use_id, None)
        return answer

    async def _dm_waiting(self, sess: Session, tool_name: str) -> None:
        """DM the session's last prompter so phone notifications work even when
        Slack is on a different channel/thread."""
        if not sess.last_user_id:
            return
        try:
            perm = await self.web.chat_getPermalink(
                channel=sess.channel, message_ts=sess.thread_ts,
            )
            link = perm.get("permalink", "")
            label = f" (`{sess.label}`)" if sess.label else ""
            await self.web.chat_postMessage(
                channel=sess.last_user_id,
                text=(f":raised_hand: Claude is waiting on you in a "
                      f"<{link}|session thread>{label} via `{tool_name}`."),
            )
        except Exception as e:
            log.debug("dm-waiting failed: %s", e)

    async def _autoname_llm(self, thread_ts: str, prompt: str) -> None:
        """Background: ask Claude for a 4-6 word thread title and update the card."""
        from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

        ask = (
            "Summarize the following user prompt as a 4-6 word title for a Slack "
            "thread. Reply with ONLY the title, no quotes, no trailing period.\n\n"
            f"PROMPT:\n{prompt[:500]}"
        )
        title = ""
        try:
            async for msg in query(
                prompt=ask,
                options=ClaudeAgentOptions(
                    cwd=self.cfg.claude.default_cwd,
                    model=self.cfg.claude.model,
                    permission_mode="bypassPermissions",
                ),
            ):
                if isinstance(msg, AssistantMessage):
                    for blk in msg.content:
                        if isinstance(blk, TextBlock):
                            title += blk.text
        except Exception as e:
            log.debug("autoname failed: %s", e)
            return
        title = title.strip().strip('"').strip("'")
        if not title:
            return
        sess = self.sessions.get(thread_ts)
        if not sess:
            return
        sess.label = title[:80]
        await self.sessions.upsert(sess)
        await self._update_card(sess)

    # ------- file staging -------

    async def _stage_files(self, event: dict, thread_ts: str) -> list[str]:
        files = event.get("files") or []
        if not files:
            return []
        dest = INBOUND_FILES_DIR / thread_ts
        dest.mkdir(parents=True, exist_ok=True)
        out: list[str] = []
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.cfg.slack.bot_token}"}
        ) as http:
            for f in files:
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                name = f.get("name") or f.get("id", "file")
                path = dest / name
                try:
                    async with http.get(url) as r:
                        path.write_bytes(await r.read())
                    out.append(str(path))
                except Exception as e:
                    log.warning("file download failed: %s", e)
        return out

    # ------- presentation helpers -------

    async def _post_card(self, sess: Session) -> None:
        if not self.cfg.features.session_card:
            return
        blocks, fallback = I.render_session_card(
            sess.session_id, sess.cwd, sess.status, sess.total_cost_usd, sess.label
        )
        resp = await self.web.chat_postMessage(
            channel=sess.channel, thread_ts=sess.thread_ts, blocks=blocks, text=fallback,
        )
        sess.card_ts = resp["ts"]
        await self.sessions.upsert(sess)

    async def _update_card(self, sess: Session) -> None:
        if not self.cfg.features.session_card or not sess.card_ts:
            return
        blocks, fallback = I.render_session_card(
            sess.session_id, sess.cwd, sess.status, sess.total_cost_usd, sess.label
        )
        try:
            await self.web.chat_update(
                channel=sess.channel, ts=sess.card_ts, blocks=blocks, text=fallback,
            )
        except Exception as e:
            log.warning("card update failed: %s", e)

    async def _react(self, channel: str, ts: str, status: str) -> None:
        emoji = R.STATUS_EMOJI.get(status)
        if not emoji:
            return
        for prev in R.STATUS_EMOJI.values():
            if prev == emoji:
                continue
            try:
                await self.web.reactions_remove(channel=channel, timestamp=ts, name=prev)
            except Exception:
                pass
        try:
            await self.web.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except Exception:
            pass

    async def _say(self, channel: str, thread_ts: str, text: str) -> None:
        await self.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    async def _upload(self, channel: str, thread_ts: str, content: str, filename: str) -> None:
        await self.web.files_upload_v2(
            channel=channel, thread_ts=thread_ts,
            filename=filename, content=content,
        )

    def _find_session_by_ts(self, ts: str) -> Session | None:
        for s in self.sessions.all():
            if s.thread_ts == ts or s.card_ts == ts:
                return s
        return None

    def _find_session_by_thread(self, thread_ts: str) -> Session | None:
        return self.sessions.get(thread_ts)

    async def start(self) -> None:
        await self._ensure_welcome_pinned()
        handler = AsyncSocketModeHandler(self.app, self.cfg.slack.app_token)
        log.info("claude-slack daemon starting; default channel=%s cwd=%s",
                 self.cfg.slack.channel_id, self.cfg.claude.default_cwd)
        await handler.start_async()

    async def _ensure_welcome_pinned(self) -> None:
        """Post + pin a usage message in the default channel, once."""
        channel = self.cfg.slack.channel_id
        if not channel:
            return
        try:
            me = await self.web.auth_test()
            bot_user = me.get("user_id", "")
            bot_name = me.get("user", "claude")
            pins = await self.web.pins_list(channel=channel)
            for item in pins.get("items", []):
                msg = item.get("message") or {}
                if msg.get("user") == bot_user and "_welcome_marker_" in (msg.get("text") or ""):
                    return  # already pinned
            blocks, fallback = _welcome_blocks(bot_name)
            resp = await self.web.chat_postMessage(channel=channel, blocks=blocks, text=fallback)
            await self.web.pins_add(channel=channel, timestamp=resp["ts"])
            log.info("posted + pinned welcome message in %s", channel)
        except Exception as e:
            log.warning("welcome pin skipped: %s", e)


def _strip_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def _autolabel(prompt: str) -> str:
    """Quick non-LLM label from the first sentence/line."""
    line = prompt.strip().splitlines()[0] if prompt.strip() else ""
    line = re.sub(r"\s+", " ", line)
    return line[:60].rstrip(".:,;-")


def _welcome_blocks(bot_name: str) -> tuple[list[dict], str]:
    """Pinned welcome message. The _welcome_marker_ token lets us detect it on restart."""
    at = f"@{bot_name}"
    text = (
        f":wave: *Welcome to {at}* — here's how to use it.  _welcome_marker_\n\n"
        f"*Start a session*\n"
        f"• `{at} <prompt>` in any channel I'm in — opens a thread, new session lives there\n"
        f"• `/claude new <prompt>` — same thing via slash command\n"
        f"• DM me — first message starts a session\n\n"
        f"*Continue a session*\n"
        f"• Just reply in the thread. No `{at}` needed.\n\n"
        f"*While I'm working*\n"
        f"• Reply with extra context — I react :eyes:, queue it, and auto-feed it on the next turn\n"
        f"• Drop a file in the thread — I stage it and tell Claude its path\n"
        f"• React :no_entry: on any of my messages — kills the session\n"
        f"• React :repeat: — replays your last prompt\n\n"
        f"*When I need your input*\n"
        f"• Status flips to :raised_hand: and I DM you with a link back to the thread\n"
        f"• `AskUserQuestion` shows radio / checkbox blocks; pick and hit *Submit*\n"
        f"• `ExitPlanMode` shows the plan with *Approve* / *Reject* buttons\n\n"
        f"*Slash commands*\n"
        f"• `/claude list` — all known sessions with status + cost\n"
        f"• `/claude new <prompt>` — start a session in a new thread\n"
        f"• `/claude kill <thread_ts>` — force shutdown\n\n"
        f"*Session card* pinned at each thread top: cwd, session id, cost.\n"
        f"Two buttons: *Interrupt* (Ctrl-C to Claude) and *Resend last* (replay last prompt)."
    )
    blocks = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": "Bridge running locally on the user's workstation. "
                               "Source: <https://github.com/samayc0616/claude-slack|claude-slack>."}]},
    ]
    return blocks, f"How to use {at}"


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    if not cfg.slack.bot_token or not cfg.slack.app_token:
        print("No config. Run: claude-slack init", flush=True)
        return 1
    daemon = Daemon(cfg)
    try:
        asyncio.run(daemon.start())
        return 0
    except KeyboardInterrupt:
        return 130
