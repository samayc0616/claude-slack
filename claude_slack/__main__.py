"""Entry point: claude-slack {init|mirror}."""
from __future__ import annotations

import argparse
import os
import sys


def cmd_init(_args) -> int:
    from . import wizard
    return wizard.run()


def cmd_mirror(args) -> int:
    """Pick the right shim based on config.
    - If [router] is configured (or CLAUDE_SLACK_ROUTER_URL is set), use the
      router-client shim.
    - Otherwise use the direct-Slack shim.
    """
    from .config import load
    cfg = load()
    has_router = cfg.router.url or os.environ.get("CLAUDE_SLACK_ROUTER_URL", "").strip()
    if has_router:
        from . import client_shim
        return client_shim.run(args.claude_args or [])
    if cfg.slack.bot_token and cfg.slack.app_token:
        from . import shim
        return shim.run(args.claude_args or [])
    sys.stderr.write(
        "claude-slack mirror: not configured.\n"
        "  Connecting through a router: set CLAUDE_SLACK_ROUTER_URL=ws://...\n"
        "  Or solo setup:               run `claude-slack init`\n"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-slack",
        description="Mirror your local Claude Code session into a private Slack DM.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Set up a personal Slack app (solo mode)")
    p_init.set_defaults(fn=cmd_init)

    p_mirror = sub.add_parser(
        "mirror",
        help="Spawn the real `claude` under a PTY and mirror to your Slack DM",
    )
    p_mirror.add_argument(
        "claude_args", nargs=argparse.REMAINDER,
        help="Arguments passed through verbatim to `claude`",
    )
    p_mirror.set_defaults(fn=cmd_mirror)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
