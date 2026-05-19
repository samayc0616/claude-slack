"""Wrap claude-agent-sdk: stream events, resume by session_id, intercept interactive tools."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import interactive as I


InteractiveHandler = Callable[[str, str, dict], Awaitable[str]]


@dataclass
class StreamEvent:
    kind: str  # text, tool_use, tool_result, result, error, thinking
    text: str = ""
    tool_name: str = ""
    tool_input: dict | None = None
    tool_use_id: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    total_tokens: int = 0
    raw: object = None


class ClaudeSession:
    """One persistent Claude conversation. One per Slack thread.

    Uses ClaudeSDKClient so we can send multiple turns without re-spawning.
    Interactive tools (AskUserQuestion, ExitPlanMode, SendUserFile) are
    intercepted via the can_use_tool callback and routed through the daemon.
    """

    def __init__(
        self,
        cwd: str,
        model: str = "",
        permission_mode: str = "bypassPermissions",
        resume: str = "",
        interactive_handler: InteractiveHandler | None = None,
    ) -> None:
        self._interactive_handler = interactive_handler

        opts_kwargs: dict = {
            "cwd": cwd,
            "permission_mode": permission_mode,
        }
        if model:
            opts_kwargs["model"] = model
        if resume:
            opts_kwargs["resume"] = resume
        if interactive_handler is not None:
            opts_kwargs["can_use_tool"] = self._can_use_tool

        self.options = ClaudeAgentOptions(**opts_kwargs)
        self.client: ClaudeSDKClient | None = None
        self.session_id: str = resume
        self.total_cost_usd: float = 0.0
        self.total_tokens: int = 0
        self._send_lock = asyncio.Lock()

    async def __aenter__(self) -> "ClaudeSession":
        self.client = ClaudeSDKClient(options=self.options)
        await self.client.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def _can_use_tool(
        self, tool_name: str, args: dict, ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if not I.is_interactive(tool_name) or self._interactive_handler is None:
            return PermissionResultAllow(updated_input=None, updated_permissions=None)
        try:
            answer = await self._interactive_handler(
                tool_name, ctx.tool_use_id or "", args
            )
        except Exception as e:
            return PermissionResultDeny(
                message=f"Slack bridge error while presenting {tool_name}: {e}",
                interrupt=False,
            )
        if tool_name == "ExitPlanMode" and answer == "__APPROVE__":
            return PermissionResultAllow(updated_input=None, updated_permissions=None)
        return PermissionResultDeny(message=answer, interrupt=False)

    async def send(self, prompt: str) -> AsyncIterator[StreamEvent]:
        """Send one user turn and yield events until the result message."""
        assert self.client is not None, "use as async context manager"
        async with self._send_lock:
            await self.client.query(prompt)
            async for msg in self.client.receive_response():
                async for ev in _decode(msg):
                    if ev.kind == "result":
                        if ev.session_id:
                            self.session_id = ev.session_id
                        self.total_cost_usd += ev.cost_usd
                        self.total_tokens += ev.total_tokens
                    yield ev

    async def interrupt(self) -> None:
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception:
                pass


async def _decode(msg) -> AsyncIterator[StreamEvent]:
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                yield StreamEvent(kind="text", text=block.text, raw=block)
            elif isinstance(block, ThinkingBlock):
                yield StreamEvent(kind="thinking", text=getattr(block, "thinking", "") or "", raw=block)
            elif isinstance(block, ToolUseBlock):
                yield StreamEvent(
                    kind="tool_use",
                    tool_name=block.name,
                    tool_input=block.input,
                    tool_use_id=block.id,
                    raw=block,
                )
    elif isinstance(msg, UserMessage):
        for block in getattr(msg, "content", []) or []:
            if isinstance(block, ToolResultBlock):
                yield StreamEvent(
                    kind="tool_result",
                    tool_use_id=block.tool_use_id,
                    text=_blockify(block.content),
                    raw=block,
                )
    elif isinstance(msg, ResultMessage):
        yield StreamEvent(
            kind="result",
            session_id=getattr(msg, "session_id", "") or "",
            cost_usd=float(getattr(msg, "total_cost_usd", 0.0) or 0.0),
            total_tokens=int(getattr(msg, "num_turns", 0) or 0),
            raw=msg,
        )


def _blockify(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)
