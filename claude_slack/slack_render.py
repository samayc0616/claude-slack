"""Render output for Slack: ANSI strip, markdown→mrkdwn, long-output snippet upload, code blocks."""
from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Slack hard limits
MAX_TEXT_BLOCK = 3000
MAX_MESSAGE = 40000
SNIPPET_THRESHOLD = 3500


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def md_to_mrkdwn(md: str) -> str:
    """Convert CommonMark-ish to Slack mrkdwn.

    Slack uses *bold* (not **bold**), _italic_, ~strike~, > quote, ``` code ```.
    Links: <url|text>. Headings get bolded since Slack ignores #.
    """
    out = strip_ansi(md)
    out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", out)
    out = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"_\1_", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", out)
    out = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", out, flags=re.MULTILINE)
    return out


def is_long(text: str) -> bool:
    return len(text) >= SNIPPET_THRESHOLD


def chunk(text: str, size: int = MAX_TEXT_BLOCK) -> list[str]:
    """Split on newlines so we don't slice mid-word."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    cur = 0
    for line in text.splitlines(keepends=True):
        if cur + len(line) > size and buf:
            chunks.append("".join(buf))
            buf, cur = [line], len(line)
        else:
            buf.append(line)
            cur += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


def code_block(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


# Status emoji for the thread root
STATUS_EMOJI = {
    "running": "hourglass_flowing_sand",
    "waiting": "raised_hand",
    "done": "white_check_mark",
    "error": "x",
    "killed": "no_entry",
}
