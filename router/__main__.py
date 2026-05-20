"""Entry point: claude-slack-router {init|run|add-user|list-users|revoke}."""
from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.panel import Panel

from .config import UsersStore, load_router

console = Console()


def cmd_init(_args) -> int:
    from . import wizard
    return wizard.run()


def cmd_run(_args) -> int:
    from . import server
    return server.run()


def cmd_add_user(args) -> int:
    store = UsersStore()
    if store.get(args.slack_user):
        console.print(f"[yellow]User {args.slack_user} already exists. Revoke first if you want a new key.[/yellow]")
        return 1
    key = store.add(args.slack_user, args.name)
    cfg = load_router()
    url = cfg.server.public_url or f"ws://<router-host>:{cfg.server.port}/v1/connect"
    console.print(Panel.fit(
        f"[bold green]User added:[/bold green] {args.name} ([bold]{args.slack_user}[/bold])\n\n"
        f"[dim]Send this snippet to them over Slack DM. Paste into "
        f"[bold]~/.config/claude-slack/config.toml[/bold]:[/dim]\n\n"
        f"[bold cyan][router][/bold cyan]\n"
        f"[bold cyan]url = \"{url}\"[/bold cyan]\n"
        f"[bold cyan]api_key = \"{key}\"[/bold cyan]\n\n"
        f"[dim]Then they run:[/dim] [bold]claude-slack mirror[/bold]",
        border_style="green",
    ))
    return 0


def cmd_list_users(_args) -> int:
    store = UsersStore()
    rows = store.list()
    if not rows:
        console.print("[dim](no users)[/dim]")
        return 0
    for u in rows:
        age = (time.time() - u.created_at) / 86400
        console.print(f"  {u.slack_user_id}  {u.name:<24}  added {age:.1f}d ago")
    return 0


def cmd_revoke(args) -> int:
    store = UsersStore()
    if not store.revoke(args.slack_user):
        console.print(f"[red]No such user:[/red] {args.slack_user}")
        return 1
    console.print(f"[green]Revoked[/green] {args.slack_user}. Their active shim (if any) will be cut on next reconnect.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-slack-router",
                                description="Shared-app router for claude-slack mirror")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Admin setup wizard").set_defaults(fn=cmd_init)
    sub.add_parser("run", help="Start the router server").set_defaults(fn=cmd_run)
    sub.add_parser("list-users", help="List provisioned users").set_defaults(fn=cmd_list_users)

    a = sub.add_parser("add-user", help="Provision a new user")
    a.add_argument("--slack-user", required=True, help="Slack user ID, e.g. U123ABC")
    a.add_argument("--name", required=True, help="Display name (for audit log)")
    a.set_defaults(fn=cmd_add_user)

    r = sub.add_parser("revoke", help="Revoke a user's access")
    r.add_argument("--slack-user", required=True)
    r.set_defaults(fn=cmd_revoke)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
