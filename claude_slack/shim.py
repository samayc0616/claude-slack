"""PTY shim: spawns the real `claude` binary under our process, forwards stdin/stdout
verbatim so your terminal experience is unchanged, and mirrors every output to a
Slack DM thread. Slack messages get written to claude's stdin as if you'd typed
them.

Run with:  claude-slack mirror [args passed to claude]
Aliasing:  alias claude='claude-slack mirror'   (optional)
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import shutil
import signal
import struct
import sys
import termios
import time
import tty
from contextlib import contextmanager
from pathlib import Path

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from . import slack_render as R
from .config import Config, load
from .redact import scrub

log = logging.getLogger("claude-slack.mirror")

# How long to wait for child output to settle before flushing the buffer to Slack.
# Short enough to feel live; long enough to avoid posting per-character noise.
FLUSH_IDLE_SECONDS = 0.6
FLUSH_MAX_BYTES = 16 * 1024
CHILD_REAP_POLL_SECONDS = 0.3


@contextmanager
def _raw_terminal(fd: int):
    """Put the user's TTY in raw cbreak mode so claude gets keystrokes verbatim."""
    if not os.isatty(fd):
        yield
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _get_winsize(fd: int) -> tuple[int, int]:
    try:
        s = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", s)
        return rows, cols
    except Exception:
        size = shutil.get_terminal_size((80, 24))
        return size.lines, size.columns


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


class Shim:
    def __init__(self, cfg: Config, claude_args: list[str]) -> None:
        self.cfg = cfg
        self.claude_args = claude_args

        self.master_fd: int = -1
        self.child_pid: int = -1
        self.exit_code: int = 0

        self.app = AsyncApp(token=cfg.slack.bot_token)
        self.web = AsyncWebClient(token=cfg.slack.bot_token)

        self._bot_user_id: str = ""
        self._bot_name: str = "claude"
        self._dm_channel: str = ""
        self._dm_user_id: str = ""
        self._thread_ts: str = ""

        self._out_buffer = bytearray()
        self._last_output_at = 0.0
        self._buffer_lock = asyncio.Lock()
        self._stop = asyncio.Event()

        self._register_handlers()

    # ---------- Slack handler registration ----------

    def _register_handlers(self) -> None:
        self.app.event("message")(self._on_slack_message)
        self.app.event("reaction_added")(self._on_slack_reaction)

    # ---------- spawn claude under a PTY ----------

    def _spawn_claude(self) -> None:
        master, slave = pty.openpty()
        rows, cols = _get_winsize(sys.stdin.fileno())
        _set_winsize(slave, rows, cols)

        pid = os.fork()
        if pid == 0:
            # Child: become claude. Use a fresh session + controlling TTY.
            try:
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            except Exception:
                pass
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if master >= 3:
                os.close(master)
            if slave >= 3:
                os.close(slave)
            os.environ["CLAUDE_SLACK_MIRROR"] = "1"
            # `claude` is a shell function in some setups; rely on the real binary
            # via the user's PATH. If that fails, fall back to npx.
            try:
                os.execvp("claude", ["claude"] + self.claude_args)
            except FileNotFoundError:
                try:
                    os.execvp("npx", ["npx", "-y", "@anthropic-ai/claude-code", *self.claude_args])
                except FileNotFoundError:
                    os.write(2, b"claude-slack mirror: cannot find `claude` on PATH\n")
                    os._exit(127)

        # Parent: wire up master fd as non-blocking.
        os.close(slave)
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.master_fd = master
        self.child_pid = pid

    # ---------- I/O loop ----------

    async def _io_loop(self) -> None:
        loop = asyncio.get_event_loop()
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        def on_stdin() -> None:
            try:
                data = os.read(stdin_fd, 4096)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

        def on_child() -> None:
            try:
                data = os.read(self.master_fd, 4096)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            try:
                os.write(stdout_fd, data)
            except OSError:
                pass
            self._out_buffer.extend(data)
            self._last_output_at = time.time()
            if len(self._out_buffer) >= FLUSH_MAX_BYTES:
                asyncio.create_task(self._flush_buffer())

        loop.add_reader(stdin_fd, on_stdin)
        loop.add_reader(self.master_fd, on_child)

        # SIGWINCH → propagate terminal resize to the child PTY.
        def on_winch(*_a) -> None:
            rows, cols = _get_winsize(stdin_fd)
            _set_winsize(self.master_fd, rows, cols)

        loop.add_signal_handler(signal.SIGWINCH, on_winch)

        flusher = asyncio.create_task(self._idle_flusher())
        reaper = asyncio.create_task(self._reap_child())

        await self._stop.wait()

        loop.remove_reader(stdin_fd)
        try:
            loop.remove_reader(self.master_fd)
        except Exception:
            pass
        flusher.cancel()
        reaper.cancel()
        await self._flush_buffer(final=True)

    async def _reap_child(self) -> None:
        while not self._stop.is_set():
            try:
                pid, status = os.waitpid(self.child_pid, os.WNOHANG)
            except ChildProcessError:
                pid, status = self.child_pid, 0
            if pid != 0:
                self.exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
                self._stop.set()
                return
            await asyncio.sleep(CHILD_REAP_POLL_SECONDS)

    # ---------- buffer flush ----------

    async def _idle_flusher(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(FLUSH_IDLE_SECONDS / 2)
            if not self._out_buffer:
                continue
            if time.time() - self._last_output_at >= FLUSH_IDLE_SECONDS:
                await self._flush_buffer()

    async def _flush_buffer(self, final: bool = False) -> None:
        async with self._buffer_lock:
            if not self._out_buffer:
                return
            raw = bytes(self._out_buffer)
            self._out_buffer.clear()

        text = raw.decode("utf-8", errors="replace")
        cleaned = _clean_for_slack(text)
        if not cleaned:
            return
        if self.cfg.features.secret_redaction:
            cleaned = scrub(cleaned)
        await self._post(cleaned, final=final)

    # ---------- Slack out ----------

    async def _ensure_thread(self) -> bool:
        """Open the DM (if needed) and post a session-starter message that becomes the thread root."""
        if self._thread_ts:
            return True
        if not self._dm_channel:
            if not self._dm_user_id:
                return False
            try:
                resp = await self.web.conversations_open(users=self._dm_user_id)
                self._dm_channel = (resp.get("channel") or {}).get("id", "")
            except Exception as e:
                log.warning("conversations.open failed: %s", e)
                return False
        if not self._dm_channel:
            return False
        try:
            args = " ".join(self.claude_args) if self.claude_args else "(no args)"
            cwd = os.getcwd()
            resp = await self.web.chat_postMessage(
                channel=self._dm_channel,
                text=(f":computer: *claude mirror session started*\n"
                      f"args: `{args}`\ncwd: `{cwd}`\nhost: `{os.uname().nodename}`"),
            )
            self._thread_ts = resp["ts"]
        except Exception as e:
            log.warning("session-start post failed: %s", e)
            return False
        return True

    async def _post(self, text: str, final: bool = False) -> None:
        if not await self._ensure_thread():
            return
        body = R.code_block(text)
        try:
            if R.is_long(body):
                await self.web.files_upload_v2(
                    channel=self._dm_channel,
                    thread_ts=self._thread_ts,
                    filename=f"claude-{int(time.time())}.txt",
                    content=text,
                )
            else:
                for chunk in R.chunk(body, size=R.MAX_MESSAGE - 50):
                    await self.web.chat_postMessage(
                        channel=self._dm_channel,
                        thread_ts=self._thread_ts,
                        text=chunk,
                    )
        except Exception as e:
            log.warning("slack post failed: %s", e)
        if final:
            try:
                await self.web.chat_postMessage(
                    channel=self._dm_channel,
                    thread_ts=self._thread_ts,
                    text=f":checkered_flag: session exited ({self.exit_code})",
                )
            except Exception:
                pass

    # ---------- Slack in ----------

    async def _on_slack_message(self, event: dict) -> None:
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        user = event.get("user", "")
        if user and self._dm_user_id and user != self._dm_user_id:
            return  # ignore other users' DMs (shouldn't happen, but safety)
        text = (event.get("text") or "").strip()
        if not text:
            return
        # Type the text + Enter into claude's PTY.
        try:
            os.write(self.master_fd, text.encode("utf-8") + b"\r")
        except OSError as e:
            log.warning("inject failed: %s", e)

    async def _on_slack_reaction(self, event: dict) -> None:
        name = event.get("reaction", "")
        if name == "no_entry":
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    # ---------- top-level lifecycle ----------

    async def _bootstrap(self) -> None:
        me = await self.web.auth_test()
        self._bot_user_id = me.get("user_id", "")
        self._bot_name = me.get("user", "claude")
        # Resolve which user this shim is for: prefer config, else fall back to channel owner.
        cfg_user = getattr(self.cfg.slack, "slack_user_id", "") or ""
        if cfg_user:
            self._dm_user_id = cfg_user
        elif self.cfg.slack.channel_id and self.cfg.slack.channel_id.startswith("D"):
            try:
                ch = await self.web.conversations_info(channel=self.cfg.slack.channel_id)
                self._dm_user_id = (ch.get("channel") or {}).get("user", "")
                self._dm_channel = self.cfg.slack.channel_id
            except Exception:
                pass

    async def start(self) -> None:
        await self._bootstrap()
        # Start Slack socket-mode in the background.
        handler = AsyncSocketModeHandler(self.app, self.cfg.slack.app_token)
        sm_task = asyncio.create_task(handler.start_async())

        self._spawn_claude()
        try:
            with _raw_terminal(sys.stdin.fileno()):
                await self._io_loop()
        finally:
            sm_task.cancel()
            try:
                await sm_task
            except (asyncio.CancelledError, Exception):
                pass


_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_CARRIAGE_RETURN_FOLD = re.compile(r"[^\n]*\r(?!\n)")  # collapse cursor-rewrite lines


def _clean_for_slack(text: str) -> str:
    """Strip ANSI + collapse spinner/cursor-rewrite noise so the Slack view is readable."""
    s = _ANSI_OSC.sub("", text)
    s = _ANSI_CSI.sub("", s)
    s = _CARRIAGE_RETURN_FOLD.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def run(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    cfg = load()
    if not cfg.slack.bot_token or not cfg.slack.app_token:
        sys.stderr.write("claude-slack mirror: no config. Run: claude-slack init\n")
        return 1
    shim = Shim(cfg, argv)
    try:
        asyncio.run(shim.start())
        return shim.exit_code
    except KeyboardInterrupt:
        return 130
