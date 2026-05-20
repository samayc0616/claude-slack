"""Slack views: App Home dashboard + modal definitions.

Each render returns the Block Kit payload for the matching Slack call:
- render_home_tab(sessions, model, default_cwd) → views.publish view
- render_new_session_modal(...) → views.open view
- render_plan_reject_modal(tool_use_id) → views.open view
- render_edit_prompt_modal(thread_ts, last_prompt) → views.open view
- render_dm_welcome(bot_name) → chat.postMessage blocks
"""
from __future__ import annotations

import time
from collections.abc import Iterable

from .sessions import Session


# ---------- App Home tab ----------

def render_home_tab(
    sessions: Iterable[Session],
    default_cwd: str,
    model: str,
) -> dict:
    rows = sorted(sessions, key=lambda s: s.last_activity, reverse=True)
    total_cost = sum(s.total_cost_usd for s in rows)

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Claude Code · Slack bridge"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f"*{len(rows)}* sessions · *${total_cost:.4f}* total · "
                           f"model `{model}` · default cwd `{default_cwd}`")}},
        {"type": "actions",
         "elements": [
             {"type": "button", "style": "primary",
              "text": {"type": "plain_text", "text": "Start new session"},
              "action_id": "home:new_session"},
             {"type": "button",
              "text": {"type": "plain_text", "text": "Refresh"},
              "action_id": "home:refresh"},
         ]},
        {"type": "divider"},
    ]

    if not rows:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "_No sessions yet. Use *Start new session* above, "
                             "or `@claude` in a channel, or DM me directly._"},
        })
        return {"type": "home", "blocks": blocks}

    status_emoji = {
        "running": ":hourglass_flowing_sand:",
        "waiting": ":raised_hand:",
        "done": ":white_check_mark:",
        "error": ":x:",
        "killed": ":no_entry:",
        "idle": ":zzz:",
    }

    now = time.time()
    for s in rows[:25]:  # Slack caps Home tab at 100 blocks; keep it sane.
        age = _humanize(now - s.last_activity)
        label = s.label or "_(no label yet)_"
        cost = f"${s.total_cost_usd:.4f}"
        emoji = status_emoji.get(s.status, ":grey_question:")
        line = (f"{emoji} *{label}*  ·  `{s.status}`  ·  {cost}  ·  _{age} ago_\n"
                f"`{s.cwd}`")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": line},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "Jump"},
                "action_id": "home:jump",
                "value": f"{s.channel}|{s.thread_ts}",
            },
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": ":no_entry: Kill"},
                 "action_id": "home:kill", "value": s.thread_ts,
                 "confirm": {
                     "title": {"type": "plain_text", "text": "Kill session?"},
                     "text": {"type": "plain_text", "text": "This sends Ctrl-C and removes the record."},
                     "confirm": {"type": "plain_text", "text": "Yes"},
                     "deny": {"type": "plain_text", "text": "No"},
                 }},
            ],
        })
        blocks.append({"type": "divider"})

    if len(rows) > 25:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn",
                                     "text": f"_…and {len(rows) - 25} older sessions (use `/claude list`)_"}]})

    return {"type": "home", "blocks": blocks}


# ---------- Modals ----------

def render_new_session_modal(default_cwd: str, model: str,
                             prefill_prompt: str = "",
                             callback_id: str = "modal:new_session") -> dict:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": "Start Claude session"},
        "submit": {"type": "plain_text", "text": "Start"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "prompt",
                "label": {"type": "plain_text", "text": "Prompt"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "min_length": 1,
                    "initial_value": prefill_prompt[:3000],
                    "placeholder": {"type": "plain_text", "text": "What should Claude do?"},
                },
            },
            {
                "type": "input",
                "block_id": "cwd",
                "label": {"type": "plain_text", "text": "Working directory"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "initial_value": default_cwd,
                },
            },
            {
                "type": "input",
                "block_id": "model",
                "optional": True,
                "label": {"type": "plain_text", "text": "Model"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "initial_option": _opt(model),
                    "options": [
                        _opt("claude-opus-4-7"),
                        _opt("claude-sonnet-4-6"),
                        _opt("claude-haiku-4-5-20251001"),
                    ],
                },
            },
        ],
    }


def render_plan_reject_modal(tool_use_id: str) -> dict:
    return {
        "type": "modal",
        "callback_id": f"modal:plan_reject:{tool_use_id}",
        "title": {"type": "plain_text", "text": "Reject plan"},
        "submit": {"type": "plain_text", "text": "Send feedback"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input",
             "block_id": "feedback",
             "label": {"type": "plain_text", "text": "What should change?"},
             "element": {
                 "type": "plain_text_input",
                 "action_id": "value",
                 "multiline": True,
                 "min_length": 1,
                 "placeholder": {"type": "plain_text",
                                 "text": "Tell Claude what's wrong so it can revise."},
             }},
        ],
    }


def render_edit_prompt_modal(thread_ts: str, last_prompt: str) -> dict:
    return {
        "type": "modal",
        "callback_id": f"modal:edit_prompt:{thread_ts}",
        "title": {"type": "plain_text", "text": "Edit & resend"},
        "submit": {"type": "plain_text", "text": "Resend"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input",
             "block_id": "prompt",
             "label": {"type": "plain_text", "text": "Edited prompt"},
             "element": {
                 "type": "plain_text_input",
                 "action_id": "value",
                 "multiline": True,
                 "min_length": 1,
                 "initial_value": last_prompt[:3000],
             }},
        ],
    }


# ---------- DM welcome ----------

def render_dm_welcome(bot_name: str) -> tuple[list[dict], str]:
    text = (f":wave: Hi! I'm {bot_name}, your bridge to local Claude Code sessions.\n"
            "Send me a message and I'll start a session. Each Slack thread = one Claude session.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions",
         "elements": [
             {"type": "button", "style": "primary",
              "text": {"type": "plain_text", "text": "Start new session"},
              "action_id": "home:new_session"},
             {"type": "button",
              "text": {"type": "plain_text", "text": "List sessions"},
              "action_id": "dm:list"},
         ]},
    ]
    return blocks, "Welcome from Claude bridge"


# ---------- helpers ----------

def _opt(label: str) -> dict:
    return {"text": {"type": "plain_text", "text": label[:75]}, "value": label[:75]}


def _humanize(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
