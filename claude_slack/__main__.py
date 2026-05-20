"""Entry point: claude-slack {init|run|list|kill}."""
from __future__ import annotations

import argparse
import json
import sys

from .config import SESSIONS_PATH
from .sessions import SessionManager


def cmd_init(args) -> int:
    from . import wizard
    return wizard.run(client=getattr(args, "client", False))


def cmd_run(_args) -> int:
    from . import daemon
    return daemon.run()


def cmd_mirror(args) -> int:
    """Auto-select transport.
    - If CLAUDE_SLACK_ROUTER_URL is set OR config has [router].url → router client mode
      (will interactively prompt for api_key if missing)
    - Else if direct Slack tokens are in config → direct mode
    - Else: error with onboarding hint
    """
    import os
    from .config import load
    cfg = load()
    has_router = cfg.router.url or os.environ.get("CLAUDE_SLACK_ROUTER_URL", "").strip()
    if has_router:
        from . import client_shim
        return client_shim.run(args.claude_args or [])
    if cfg.slack.bot_token and cfg.slack.app_token:
        from . import shim
        return shim.run(args.claude_args or [])
    import sys as _s
    _s.stderr.write(
        "claude-slack mirror: not configured.\n"
        "  Team (router) mode: export CLAUDE_SLACK_ROUTER_URL=wss://... then re-run.\n"
        "  Solo mode:          claude-slack init\n"
    )
    return 1


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

    p_init = sub.add_parser("init", help="Run the setup wizard")
    p_init.add_argument("--client", action="store_true",
                         help="Client mode: connect to a shared router instead of creating a personal Slack app")
    p_init.set_defaults(fn=cmd_init)
    sub.add_parser("run", help="Start the Slack-spawned daemon (legacy)").set_defaults(fn=cmd_run)
    sub.add_parser("list", help="List known sessions").set_defaults(fn=cmd_list)

    p_mirror = sub.add_parser(
        "mirror", help="Spawn `claude` under a PTY and mirror to Slack",
    )
    p_mirror.add_argument("claude_args", nargs=argparse.REMAINDER,
                           help="Args passed through to the underlying `claude` binary")
    p_mirror.set_defaults(fn=cmd_mirror)

    p_kill = sub.add_parser("kill", help="Forget a session record")
    p_kill.add_argument("thread_ts")
    p_kill.set_defaults(fn=cmd_kill)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
