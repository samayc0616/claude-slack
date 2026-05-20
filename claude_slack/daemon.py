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
from . import views as V
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
        self._injections: dict[str, list[tuple[str, str]]] = {}  # thread_ts → [(msg_ts, text)]
        self._dm_welcomed: set[str] = set()          # user_ids we've greeted in DM already
        self._assistant_threads: set[str] = set()    # thread_ts created via Assistant container
        self._bot_user_id: str = ""                  # filled at startup via auth.test
        self._bot_name: str = "claude"
        self._register_handlers()

    # ------- event registration -------

    def _register_handlers(self) -> None:
        # Events
        self.app.event("app_mention")(self.on_mention)
        self.app.event("message")(self.on_message)
        self.app.event("reaction_added")(self.on_reaction)
        self.app.event("app_home_opened")(self.on_app_home_opened)
        self.app.event("assistant_thread_started")(self.on_assistant_thread_started)
        self.app.event("assistant_thread_context_changed")(self.on_assistant_thread_context_changed)
        self.app.command("/claude")(self.on_slash)

        # Shortcuts
        self.app.shortcut("msg_send_to_claude")(self.on_send_to_claude_shortcut)
        self.app.shortcut("global_new_session")(self.on_global_start_shortcut)

        # Buttons / interactive components
        self.app.action("btn:interrupt")(self.on_interrupt_btn)
        self.app.action("btn:resend")(self.on_resend_btn)
        self.app.action(re.compile(r"^auq:[^:]+:\d+$"))(self.on_auq_select)
        self.app.action(re.compile(r"^auq_submit:.+$"))(self.on_auq_submit)
        self.app.action(re.compile(r"^auq_cancel:.+$"))(self.on_auq_cancel)
        self.app.action(re.compile(r"^plan_approve:.+$"))(self.on_plan_approve)
        self.app.action(re.compile(r"^plan_reject:.+$"))(self.on_plan_reject)

        # Home tab buttons
        self.app.action("home:new_session")(self.on_home_new_session)
        self.app.action("home:refresh")(self.on_home_refresh)
        self.app.action("home:jump")(self.on_home_jump)
        self.app.action("home:kill")(self.on_home_kill)
        self.app.action("dm:list")(self.on_dm_list)

        # Modal submissions
        self.app.view("modal:new_session")(self.on_modal_new_session)
        self.app.view(re.compile(r"^modal:plan_reject:.+$"))(self.on_modal_plan_reject)
        self.app.view(re.compile(r"^modal:edit_prompt:.+$"))(self.on_modal_edit_prompt)

    # ------- event handlers -------

    async def on_mention(self, event: dict, say) -> None:
        text = _strip_mentions(event.get("text", ""))
        channel = event["channel"]
        is_existing_thread = bool(event.get("thread_ts"))
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")

        # Mention inside a pre-existing thread: pull recent messages above us as context.
        if is_existing_thread and not self.sessions.get(thread_ts):
            ctx = await self._read_thread_context(channel, thread_ts, before_ts=event["ts"])
            if ctx:
                text = ctx + "\n\nUser:\n" + text

        paths = await self._stage_files(event, thread_ts)
        if paths:
            text += "\n\nFiles attached:\n" + "\n".join(f"- {p}" for p in paths)
        await self._dispatch(channel, thread_ts, text, parent_ts=event["ts"], user_id=user_id)

    async def on_message(self, event: dict, say) -> None:
        # Handle edits first (they have subtype=message_changed).
        if event.get("subtype") == "message_changed":
            await self._on_message_edited(event)
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text", "") or ""
        user_id = event.get("user", "")
        channel = event["channel"]
        thread_ts = event.get("thread_ts")
        channel_type = event.get("channel_type", "")

        # DM first-message: greet, then proceed.
        if channel_type == "im" and not thread_ts:
            if user_id and user_id not in self._dm_welcomed:
                self._dm_welcomed.add(user_id)
                blocks, fb = V.render_dm_welcome(self._bot_name)
                try:
                    await self.web.chat_postMessage(channel=channel, blocks=blocks, text=fb)
                except Exception:
                    pass
            # DM body becomes the first turn in a new thread (the message itself).
            thread_ts = event["ts"]

        if not thread_ts:
            return
        # In channels, only react to replies in threads we own. In DMs, any reply is ours.
        if channel_type != "im" and not self.sessions.get(thread_ts):
            return

        paths = await self._stage_files(event, thread_ts)
        if paths:
            text += "\n\nFiles attached:\n" + "\n".join(f"- {p}" for p in paths)
        await self._dispatch(channel, thread_ts, text, parent_ts=thread_ts, user_id=user_id)

    async def _on_message_edited(self, event: dict) -> None:
        """Handle subtype=message_changed. Behavior depends on session state."""
        msg = event.get("message") or {}
        prev = event.get("previous_message") or {}
        channel = event["channel"]
        edited_text = (msg.get("text") or "").strip()
        prev_text = (prev.get("text") or "").strip()
        if not edited_text or edited_text == prev_text:
            return
        if msg.get("bot_id"):
            return
        edited_ts = msg.get("ts") or ""
        user_id = msg.get("user", "")
        thread_ts = msg.get("thread_ts") or edited_ts
        sess = self.sessions.get(thread_ts)
        if not sess:
            return

        # 1) Edit lands on a queued message? Replace the matching ts in the queue.
        queue = self._injections.get(thread_ts, [])
        for i, (qts, _t) in enumerate(queue):
            if qts == edited_ts:
                queue[i] = (qts, edited_text)
                await self._ephemeral(
                    channel, thread_ts, user_id,
                    ":pencil2: queued message updated to use the edited version.",
                )
                return

        # 2) Edit lands on the LAST prompt we sent to Claude.
        if sess.last_user_msg_ts == edited_ts:
            if sess.status == "running":
                # Stop + reprompt.
                client = self._claude.get(thread_ts)
                if client:
                    await client.interrupt()
                await self._say(
                    channel, thread_ts,
                    ":pencil2: *you edited your message — interrupting and reprompting.*",
                )
                reprompt = (
                    "Note: the user edited their previous message. Disregard the prior "
                    "version and use this corrected prompt instead.\n\n"
                    f"OLD:\n{prev_text}\n\nCORRECTED:\n{edited_text}"
                )
                # Fire as a follow-up turn. We're still inside the lock from the
                # interrupted turn, so spawn a task to re-enter _handle_turn cleanly.
                asyncio.create_task(self._handle_turn(
                    channel, thread_ts, reprompt, parent_ts=thread_ts, user_id=user_id,
                ))
                return
            if sess.status == "waiting":
                # Don't interrupt waiting state; just note it.
                await self._ephemeral(
                    channel, thread_ts, user_id,
                    ":pencil2: edit noted, but I'm waiting on your input. Pick an option above to proceed.",
                )
                return
            # done / idle / error → reprompt as a new turn.
            await self._say(
                channel, thread_ts,
                ":pencil2: *picked up your edit — running again with the corrected version.*",
            )
            reprompt = (
                f"The user edited their previous message. Please redo your response "
                f"using the corrected version.\n\nOLD:\n{prev_text}\n\nCORRECTED:\n{edited_text}"
            )
            await self._handle_turn(channel, thread_ts, reprompt,
                                    parent_ts=thread_ts, user_id=user_id)
            return

        # 3) Edit to an older message: too late to undo, just acknowledge.
        await self._ephemeral(
            channel, thread_ts, user_id,
            ":pencil2: edit noted, but Claude has already processed that message.",
        )

    async def _dispatch(self, channel: str, thread_ts: str, text: str,
                        parent_ts: str, user_id: str) -> None:
        """Route an incoming user message: queue if busy, otherwise process now."""
        text = text.strip()
        if not text:
            return
        sess = self.sessions.get(thread_ts)
        if sess and sess.status in ("running", "waiting"):
            # Busy. Queue for the follow-up turn so the user feels heard immediately.
            self._injections.setdefault(thread_ts, []).append((parent_ts, text))
            try:
                await self.web.reactions_add(
                    channel=channel, timestamp=parent_ts, name="eyes",
                )
            except Exception:
                pass
            # Ephemeral so this notice is only visible to the user who sent it.
            await self._ephemeral(
                channel, thread_ts, user_id,
                f":inbox_tray: queued for the next turn ({len(self._injections[thread_ts])} pending)",
            )
            if user_id:
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
        reactor = event.get("user", "")

        # Map reaction → action. Skip reactions from the bot itself.
        if reactor == self._bot_user_id:
            return
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
        elif name == "pencil2" or name == "memo":
            # Open the edit-prompt modal. Requires a trigger_id, which the reaction event
            # does not carry, so we just post an ephemeral hint to use the resend button.
            await self._ephemeral(
                channel, sess.thread_ts, reactor,
                "Use the *Resend last* button on the session card to edit and resend "
                "(Slack reaction events don't carry a trigger_id, so we can't open a modal here).",
            )
        elif name == "clipboard":
            await self._export_transcript(sess, reactor)

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
        trigger_id = body.get("trigger_id", "")
        if not trigger_id:
            # Fall back to immediate rejection if we lost the trigger_id somehow.
            self._resolve(tool_use_id, "User rejected the plan in Slack.")
            return
        try:
            await self.web.views_open(
                trigger_id=trigger_id,
                view=V.render_plan_reject_modal(tool_use_id),
            )
        except Exception as e:
            log.warning("plan reject modal failed: %s", e)
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
                     "\n".join(f"- {m}" for _ts, m in queued)
        self._last_prompt[thread_ts] = prompt
        sess.last_user_msg_ts = parent_ts
        await self.sessions.upsert(sess)

        async with self.sessions.lock(thread_ts):
            await self.sessions.set_status(thread_ts, "running")
            await self._react(channel, parent_ts, "running")
            await self._set_assistant_status(channel, thread_ts, "is thinking…")
            try:
                await self._run(sess, prompt)
                # If new messages arrived while running, immediately run a follow-up turn
                # so the user doesn't have to re-mention.
                follow_up = self._injections.pop(thread_ts, [])
                if follow_up:
                    follow_prompt = "Additional context from Slack:\n" + \
                                    "\n".join(f"- {m}" for _ts, m in follow_up)
                    await self._set_assistant_status(channel, thread_ts, "incorporating new context…")
                    await self._run(sess, follow_prompt)
                await self.sessions.set_status(thread_ts, "done")
                await self._react(channel, parent_ts, "done")
                await self._set_assistant_status(channel, thread_ts, "")
                await self._set_assistant_prompts(channel, thread_ts, [
                    {"title": "Keep going", "message": "Continue with the next step."},
                    {"title": "Summarize", "message": "Give me a 3-bullet summary of what you just did."},
                    {"title": "What's next?", "message": "What would you do next?"},
                ])
            except Exception as e:
                log.exception("session %s failed", thread_ts)
                await self._say(channel, thread_ts, f":x: *bridge error*\n```{e}```")
                await self.sessions.set_status(thread_ts, "error")
                await self._react(channel, parent_ts, "error")
                await self._set_assistant_status(channel, thread_ts, "")
        # Refresh Home tab for the prompter so the dashboard reflects this turn.
        if user_id:
            asyncio.create_task(self._publish_home(user_id))

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
        await self._set_assistant_status(ch, ts, f"waiting on your input ({tool_name})…")

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

    # ------- App Home tab -------

    async def on_app_home_opened(self, event: dict) -> None:
        if event.get("tab") != "home":
            return
        await self._publish_home(event.get("user", ""))

    async def _publish_home(self, user_id: str) -> None:
        if not user_id:
            return
        try:
            view = V.render_home_tab(
                self.sessions.all(),
                self.cfg.claude.default_cwd,
                self.cfg.claude.model,
            )
            await self.web.views_publish(user_id=user_id, view=view)
        except Exception as e:
            log.debug("home publish failed: %s", e)

    async def on_home_new_session(self, ack, body) -> None:
        await ack()
        await self._open_new_session_modal(body.get("trigger_id", ""))

    async def on_home_refresh(self, ack, body) -> None:
        await ack()
        await self._publish_home(body.get("user", {}).get("id", ""))

    async def on_home_jump(self, ack, body) -> None:
        await ack()
        # Slack handles deep linking when the user just clicks Jump-marked sessions
        # via the action_id; we don't need to do anything server-side. This handler
        # exists so Bolt doesn't log "unhandled action".

    async def on_home_kill(self, ack, body) -> None:
        await ack()
        thread_ts = body["actions"][0].get("value", "")
        if not thread_ts:
            return
        client = self._claude.pop(thread_ts, None)
        if client:
            try:
                await client.interrupt()
                await client.__aexit__(None, None, None)
            except Exception:
                pass
        await self.sessions.remove(thread_ts)
        await self._publish_home(body.get("user", {}).get("id", ""))

    async def on_dm_list(self, ack, body) -> None:
        await ack()
        rows = self.sessions.all()
        if not rows:
            text = "_(no sessions yet)_"
        else:
            text = "\n".join(
                f"• `{s.thread_ts}` {s.status} ${s.total_cost_usd:.4f} "
                + (f"_{s.label}_" if s.label else "")
                for s in rows[:25]
            )
        ch = body.get("channel", {}).get("id", "")
        if ch:
            await self.web.chat_postMessage(channel=ch, text=text)

    # ------- shortcuts -------

    async def on_send_to_claude_shortcut(self, ack, body) -> None:
        """Right-click any message → Send to Claude. Pre-fills modal with that text."""
        await ack()
        msg = body.get("message") or {}
        snippet = msg.get("text") or ""
        # Optionally include a reference to the original message.
        author = msg.get("user", "")
        if author:
            snippet = f"<@{author}> said:\n{snippet}"
        await self._open_new_session_modal(body.get("trigger_id", ""), prefill=snippet)

    async def on_global_start_shortcut(self, ack, body) -> None:
        await ack()
        await self._open_new_session_modal(body.get("trigger_id", ""))

    async def _open_new_session_modal(self, trigger_id: str, prefill: str = "") -> None:
        if not trigger_id:
            return
        try:
            await self.web.views_open(
                trigger_id=trigger_id,
                view=V.render_new_session_modal(
                    default_cwd=self.cfg.claude.default_cwd,
                    model=self.cfg.claude.model,
                    prefill_prompt=prefill,
                ),
            )
        except Exception as e:
            log.warning("open new-session modal failed: %s", e)

    # ------- modal submissions -------

    async def on_modal_new_session(self, ack, body) -> None:
        await ack()
        values = body["view"]["state"]["values"]
        prompt = values["prompt"]["value"]["value"]
        cwd = values["cwd"]["value"]["value"]
        model_pick = (values.get("model", {}).get("value", {}).get("selected_option") or {}).get("value", "")
        user_id = body.get("user", {}).get("id", "")
        channel = self.cfg.slack.channel_id
        if not channel:
            log.warning("no default channel set; cannot post new session")
            return
        # Post the thread-starter, then run the turn.
        resp = await self.web.chat_postMessage(
            channel=channel,
            text=f"<@{user_id}> {prompt}",
        )
        thread_ts = resp["ts"]
        sess = Session(thread_ts=thread_ts, channel=channel, cwd=cwd,
                       last_user_id=user_id)
        sess.label = _autolabel(prompt) if self.cfg.features.auto_name_threads else ""
        await self.sessions.upsert(sess)
        await self._post_card(sess)
        if model_pick:
            # Override per-session model if the user picked one in the modal.
            # ClaudeSession reads from cfg at construction time; we accept this as a
            # per-bridge default for now and just log mismatches.
            log.info("modal requested model=%s (using bridge default for this session)", model_pick)
        await self._handle_turn(channel, thread_ts, prompt, parent_ts=thread_ts, user_id=user_id)

    async def on_modal_plan_reject(self, ack, body) -> None:
        await ack()
        callback_id = body["view"]["callback_id"]
        tool_use_id = callback_id.split(":", 2)[2]
        feedback = body["view"]["state"]["values"]["feedback"]["value"]["value"]
        self._resolve(tool_use_id, f"User rejected the plan with feedback: {feedback}")

    async def on_modal_edit_prompt(self, ack, body) -> None:
        await ack()
        callback_id = body["view"]["callback_id"]
        thread_ts = callback_id.split(":", 2)[2]
        new_prompt = body["view"]["state"]["values"]["prompt"]["value"]["value"]
        sess = self.sessions.get(thread_ts)
        if not sess:
            return
        await self._handle_turn(sess.channel, thread_ts, new_prompt, parent_ts=thread_ts,
                                user_id=body.get("user", {}).get("id", ""))

    # ------- assistant (AI Apps) -------

    async def on_assistant_thread_started(self, event: dict) -> None:
        thread = event.get("assistant_thread") or {}
        ch = thread.get("channel_id", "")
        ts = thread.get("thread_ts", "")
        user_id = thread.get("user_id", "")
        if ts:
            self._assistant_threads.add(ts)
        await self._set_assistant_prompts(ch, ts, [
            {"title": "Start a coding task", "message": "Help me with: "},
            {"title": "Resume my last session", "message": "Resume my most recent session and continue."},
            {"title": "What can you do?", "message": "What can you do here?"},
        ])
        # Track the user so DM-when-waiting works for assistant-style threads.
        if user_id and ts:
            sess = self.sessions.get(ts)
            if sess:
                sess.last_user_id = user_id
                await self.sessions.upsert(sess)

    async def on_assistant_thread_context_changed(self, event: dict) -> None:
        # Slack tells us when the user switches the assistant's channel context.
        # We don't act on it yet, but registering the handler avoids Bolt warnings.
        return

    async def _set_assistant_status(self, channel: str, thread_ts: str, status: str) -> None:
        """No-op outside assistant threads. Best-effort."""
        if thread_ts not in self._assistant_threads:
            return
        try:
            await self.web.api_call(
                "assistant.threads.setStatus",
                params={"channel_id": channel, "thread_ts": thread_ts, "status": status},
            )
        except Exception as e:
            log.debug("setStatus failed: %s", e)

    async def _set_assistant_prompts(self, channel: str, thread_ts: str,
                                      prompts: list[dict]) -> None:
        if thread_ts not in self._assistant_threads:
            return
        try:
            await self.web.api_call(
                "assistant.threads.setSuggestedPrompts",
                params={"channel_id": channel, "thread_ts": thread_ts,
                        "prompts": prompts},
            )
        except Exception as e:
            log.debug("setSuggestedPrompts failed: %s", e)

    # ------- thread context preload -------

    async def _read_thread_context(self, channel: str, thread_ts: str,
                                    before_ts: str, limit: int = 20) -> str:
        """Read up to `limit` messages from before our mention to seed Claude's context."""
        try:
            resp = await self.web.conversations_replies(
                channel=channel, ts=thread_ts, limit=limit,
            )
        except Exception as e:
            log.debug("read thread context failed: %s", e)
            return ""
        msgs = resp.get("messages", []) or []
        lines: list[str] = []
        for m in msgs:
            if m.get("ts") == before_ts:
                break
            if m.get("bot_id"):
                continue
            user = m.get("user", "?")
            text = (m.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"<@{user}>: {text}")
        if not lines:
            return ""
        return "Earlier in this Slack thread:\n" + "\n".join(lines)

    # ------- transcript export (:clipboard: reaction) -------

    async def _export_transcript(self, sess: Session, reactor: str) -> None:
        try:
            resp = await self.web.conversations_replies(
                channel=sess.channel, ts=sess.thread_ts, limit=999,
            )
        except Exception as e:
            log.warning("transcript fetch failed: %s", e)
            return
        msgs = resp.get("messages", []) or []
        lines: list[str] = [
            f"# Claude session transcript",
            f"thread_ts: {sess.thread_ts}",
            f"session_id: {sess.session_id}",
            f"cwd: {sess.cwd}",
            f"label: {sess.label}",
            f"cost: ${sess.total_cost_usd:.4f}",
            "",
        ]
        for m in msgs:
            who = m.get("user") or m.get("bot_id") or "?"
            ts = m.get("ts", "")
            text = (m.get("text") or "").strip()
            lines.append(f"## {who} @ {ts}")
            lines.append(text)
            lines.append("")
        content = "\n".join(lines)
        try:
            await self.web.files_upload_v2(
                channel=sess.channel,
                thread_ts=sess.thread_ts,
                filename=f"transcript-{sess.thread_ts}.md",
                content=content,
                initial_comment=f"<@{reactor}> transcript export",
            )
        except Exception as e:
            log.warning("transcript upload failed: %s", e)

    # ------- ephemeral helper -------

    async def _ephemeral(self, channel: str, thread_ts: str, user: str, text: str) -> None:
        if not user:
            await self._say(channel, thread_ts, text)
            return
        try:
            await self.web.chat_postEphemeral(
                channel=channel, thread_ts=thread_ts, user=user, text=text,
            )
        except Exception:
            # Fall back to a normal message if ephemeral fails (e.g. in DMs).
            await self._say(channel, thread_ts, text)

    async def start(self) -> None:
        try:
            me = await self.web.auth_test()
            self._bot_user_id = me.get("user_id", "")
            self._bot_name = me.get("user", "claude")
        except Exception as e:
            log.warning("auth_test on startup failed: %s", e)
        await self._ensure_welcome_pinned()
        await self._ensure_channel_bookmark()
        handler = AsyncSocketModeHandler(self.app, self.cfg.slack.app_token)
        log.info("claude-slack daemon starting as @%s; default channel=%s cwd=%s",
                 self._bot_name, self.cfg.slack.channel_id, self.cfg.claude.default_cwd)
        await handler.start_async()

    async def _ensure_channel_bookmark(self) -> None:
        channel = self.cfg.slack.channel_id
        if not channel:
            return
        try:
            existing = await self.web.bookmarks_list(channel_id=channel)
            for b in existing.get("bookmarks", []) or []:
                if b.get("title") == "claude-slack docs":
                    return
            await self.web.bookmarks_add(
                channel_id=channel,
                title="claude-slack docs",
                type="link",
                link="https://github.com/samayc0616/claude-slack",
                emoji=":books:",
            )
        except Exception as e:
            log.debug("bookmark add skipped: %s", e)

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
