"""Interactive UI mapping.

AskUserQuestion → Block Kit radio_buttons / checkboxes
ExitPlanMode → plan text + approve/reject buttons
SendUserFile → upload to Slack thread

The daemon's interactive_handler creates a Future, posts blocks to Slack with
action_ids carrying the tool_use_id, awaits the Future. The daemon's action
handler resolves the Future when the user clicks. The can_use_tool callback
then returns the answer as a PermissionResultDeny message so Claude sees it.
"""
from __future__ import annotations

INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode", "SendUserFile"}


def is_interactive(tool_name: str) -> bool:
    return tool_name in INTERACTIVE_TOOLS


def _opt(label: str, value: str | None = None) -> dict:
    v = (value if value is not None else label)[:75]
    return {"text": {"type": "plain_text", "text": label[:75]}, "value": v}


def render_ask_user_question(tool_use_id: str, args: dict) -> tuple[list[dict], str]:
    """Return (blocks, fallback_text). One action_id per question: `auq:{tid}:{i}`."""
    questions = args.get("questions") or []
    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": ":speech_balloon: *Claude is asking:*"}},
    ]
    for i, q in enumerate(questions):
        opts = q.get("options") or []
        choices = [_opt(o.get("label", "?")) for o in opts] or [_opt("Continue")]
        # Always offer an Other option so the user can free-type.
        choices.append(_opt("Other (type a reply below)"))
        element_type = "checkboxes" if q.get("multiSelect") else "radio_buttons"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*{q.get('header','?')}* — {q.get('question','')}"},
        })
        blocks.append({
            "type": "actions",
            "block_id": f"auq:{tool_use_id}:{i}",
            "elements": [{
                "type": element_type,
                "options": choices,
                "action_id": f"auq:{tool_use_id}:{i}",
            }],
        })
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "style": "primary",
             "text": {"type": "plain_text", "text": "Submit"},
             "action_id": f"auq_submit:{tool_use_id}"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "Cancel"},
             "action_id": f"auq_cancel:{tool_use_id}"},
        ],
    })
    return blocks, "Claude is asking a question"


def render_exit_plan_mode(tool_use_id: str, args: dict) -> tuple[list[dict], str]:
    plan = (args.get("plan") or "")[:2900]
    blocks = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": ":scroll: *Claude proposes a plan:*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": plan}},
        {"type": "actions",
         "elements": [
             {"type": "button", "style": "primary",
              "text": {"type": "plain_text", "text": "Approve"},
              "action_id": f"plan_approve:{tool_use_id}"},
             {"type": "button", "style": "danger",
              "text": {"type": "plain_text", "text": "Reject"},
              "action_id": f"plan_reject:{tool_use_id}"},
         ]},
    ]
    return blocks, "Claude is asking to leave plan mode"


def render_session_card(session_id: str, cwd: str, status: str,
                       cost: float, label: str = "") -> tuple[list[dict], str]:
    """Pinned card at thread top. Includes the interrupt button."""
    line = f"*session* `{session_id or 'new'}`  ·  *status* `{status}`  ·  *cost* ${cost:.4f}"
    if label:
        line = f"*{label}*\n" + line
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": line}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"`{cwd}`"}]},
        {"type": "actions",
         "elements": [
             {"type": "button", "style": "danger",
              "text": {"type": "plain_text", "text": "Interrupt"},
              "action_id": "btn:interrupt", "confirm": {
                  "title": {"type": "plain_text", "text": "Interrupt session?"},
                  "text": {"type": "plain_text", "text": "Sends Ctrl-C to Claude."},
                  "confirm": {"type": "plain_text", "text": "Yes"},
                  "deny": {"type": "plain_text", "text": "No"},
              }},
             {"type": "button",
              "text": {"type": "plain_text", "text": "Resend last"},
              "action_id": "btn:resend"},
         ]},
    ]
    return blocks, line
