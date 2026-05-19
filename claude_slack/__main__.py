"""Entry point: claude-slack {init|run|list|kill}."""
from __future__ import annotations

import argparse
import json
import sys

from .config import SESSIONS_PATH
from .sessions import SessionManager


def cmd_init(_args) -> int:
    from . import wizard
    return wizard.run()


def cmd_run(_args) -> int:
    from . import daemon
    return daemon.run()


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
    """Remove a session record. Won't reach a running daemon; pair with bot reaction shortcuts."""
    import asyncio
    mgr = SessionManager()
    if not mgr.get(args.thread_ts):
        print(f"no session for thread {args.thread_ts}")
        return 1
    asyncio.run(mgr.remove(args.thread_ts))
    print(f"removed {args.thread_ts}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-slack", description="Slack bridge for Claude Code")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Run the setup wizard").set_defaults(fn=cmd_init)
    sub.add_parser("run", help="Start the bridge daemon").set_defaults(fn=cmd_run)
    sub.add_parser("list", help="List known sessions").set_defaults(fn=cmd_list)

    p_kill = sub.add_parser("kill", help="Forget a session record")
    p_kill.add_argument("thread_ts")
    p_kill.set_defaults(fn=cmd_kill)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
