"""Wire protocol between router and per-user shims. JSON over WSS at /v1/connect."""
from __future__ import annotations

import json


# Methods the shim is allowed to call through the router. Anything not on this
# list returns `method_not_allowed`. The router rewrites channel/user fields
# to constrain them to the caller's bound Slack user.
ALLOWED_METHODS = frozenset({
    "auth.test",
    "chat.postMessage",
    "chat.postEphemeral",
    "chat.update",
    "chat.getPermalink",
    "files.upload_v2",
    "files_upload_v2",
    "reactions.add",
    "reactions.remove",
    "conversations.open",
    "conversations.info",
    "conversations.replies",
})


def hello(api_key: str, shim_version: str) -> dict:
    return {"type": "hello", "api_key": api_key, "shim_version": shim_version}


def welcome(slack_user_id: str, bot_user_id: str, bot_name: str,
            dm_channel_id: str = "") -> dict:
    return {
        "type": "welcome",
        "slack_user_id": slack_user_id,
        "bot_user_id": bot_user_id,
        "bot_name": bot_name,
        "dm_channel_id": dm_channel_id,
    }


def auth_error(reason: str) -> dict:
    return {"type": "auth_error", "reason": reason}


def event(payload: dict) -> dict:
    return {"type": "event", "payload": payload}


def api_call(request_id: str, method: str, params: dict) -> dict:
    return {"type": "api_call", "request_id": request_id,
            "method": method, "params": params}


def api_response(request_id: str, ok: bool, response: dict | None = None,
                 error: str = "") -> dict:
    out: dict = {"type": "api_response", "request_id": request_id, "ok": ok}
    if response is not None:
        out["response"] = response
    if error:
        out["error"] = error
    return out


def turn_complete(thread_ts: str, cost_usd: float, num_turns: int) -> dict:
    return {"type": "turn_complete", "thread_ts": thread_ts,
            "cost_usd": cost_usd, "num_turns": num_turns}


def ping() -> dict:
    return {"type": "ping"}


def pong() -> dict:
    return {"type": "pong"}


def encode(msg: dict) -> str:
    return json.dumps(msg)


def decode(raw: str) -> dict:
    return json.loads(raw)
