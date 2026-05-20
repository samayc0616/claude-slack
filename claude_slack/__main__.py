"""Entry point: claude-slack {init|mirror|run|list|kill}."""
from __future__ import annotations

import argparse
import sys

from .config import SESSIONS_PATH
from .sessions import SessionManager


def cmd_init(_args) -> int:
    from . import wizard
    return wizard.run()


def cmd_run(_args) -> int:
    from . import daemon
    return daemon.run()


def cmd_mirror(args) -> int:
    from .config import load
    cfg = load()
    if not (cfg.slack.bot_token and cfg.slack.app_token):
        sys.stderr.write("claude-slack mirror: not configured. Run: claude-slack init\n")
        return 1
    from . import shim
    return shim.run(args.claude_args or [])


def cmd_list(_args) -> int:
    if not SESSIONS_PATH.exists():
        print("(no sessions)")
        return 0
    mgr = SessionManager()
    rows = mgr.all()
    if not rows:
        print("(no sessions)")
        return 0
    for s in rows:
        print(f"{s.status:<8} {s.thread_ts:<20} {s.session_id or '-':<38} cost=${s.total_cost_usd:.4f} cwd={s.cwd}")
    return 0


def cmd_kill(args) -> int:
    import asyncio
    mgr = SessionManager()
    if not mgr.get(args.thread_ts):
        print(f"no session for thread {args.thread_ts}")
        return 1
    asyncio.run(mgr.remove(args.thread_ts))
    print(f"removed {args.thread_ts}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-slack", description="Slack mirror for local Claude Code sessions")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Run the setup wizard").set_defaults(fn=cmd_init)

    p_mirror = sub.add_parser(
        "mirror", help="Spawn `claude` under a PTY and mirror to your Slack DM",
    )
    p_mirror.add_argument("claude_args", nargs=argparse.REMAINDER,
                           help="Args passed through to the underlying `claude` binary")
    p_mirror.set_defaults(fn=cmd_mirror)

    sub.add_parser("run", help="Start the Slack-spawned daemon (legacy)").set_defaults(fn=cmd_run)
    sub.add_parser("list", help="List known sessions").set_defaults(fn=cmd_list)

    p_kill = sub.add_parser("kill", help="Forget a session record")
    p_kill.add_argument("thread_ts")
    p_kill.set_defaults(fn=cmd_kill)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
