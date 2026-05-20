# claude-slack

Claude Code's Remote Control feature, rebuilt with Slack as the transport. Run `claude` in your terminal as normal — every assistant message, tool call, and your prompts get mirrored into a private Slack DM with the bot. Type in Slack from your phone or another machine; that text becomes claude's next prompt as if you'd typed it yourself.

**One shared bot for the team. The router runs on the admin's machine and demultiplexes Slack events to each teammate's local mirror.** Each user's DM with the bot is private (Slack-enforced). Each user's prompts and Claude responses execute on their own machine.

## How it works

```
                    ┌─────────┐
                    │  Slack  │
                    └────┬────┘
                         │ single socket-mode connection
                         ▼
              ┌──────────────────────┐
              │  router  (one host)  │   ◄── runs on admin's workstation,
              │                      │       holds the shared bot token,
              │  • api-key registry  │       fans events to user shims
              │  • slack ↔ user map  │
              └──┬───────────────┬───┘
   outbound WSS  │               │  outbound WSS
   from shim     │               │  from shim
                 ▼               ▼
       ┌────────────┐    ┌────────────┐
       │ samay's    │    │ alice's    │
       │ workstation│    │ laptop     │
       │            │    │            │
       │  claude-   │    │  claude-   │
       │  slack     │    │  slack     │
       │  mirror    │    │  mirror    │
       │     │      │    │     │      │
       │     ▼      │    │     ▼      │
       │ real       │    │ real       │
       │ `claude`   │    │ `claude`   │  ← real Anthropic binary,
       │ in a PTY   │    │ in a PTY   │     terminal works as normal,
       └────────────┘    └────────────┘     local sessions stay local
```

## Setup

### Admin (you, once)

```bash
git clone https://github.com/samayc0616/claude-slack ~/claude-slack
cd ~/claude-slack && uv sync
uv run claude-slack-router init    # walks you through Slack app creation
uv run claude-slack-router run     # keep running (tmux / systemd)
```

The init wizard creates ONE Slack app for the whole team. Anyone in the workspace can DM `@claude`. The router process holds the single socket-mode connection.

**The router lives on your machine for now.** Realistically that means:
- Your machine needs to be on and network-reachable when teammates want to use the bot
- Other users' shims dial in over WebSocket — they need a route to your host
- Put the router on a real shared box once you're past prototyping

#### Network reachability

Teammates' shims need to reach your router. Common setups:

| Setup | Router URL example |
|---|---|
| Everyone on the corp network with stable internal DNS | `ws://your-host.corp:31415/v1/connect` |
| Tailscale mesh between you and teammates | `ws://samay-laptop.tail-net.ts.net:31415/v1/connect` |
| Public IP + a reverse proxy doing TLS (nginx/caddy) | `wss://router.example.com/v1/connect` |
| Cloudflare Tunnel from your laptop | `wss://router.your-domain.com/v1/connect` |

For testing solo: just `ws://localhost:31415/v1/connect`.

### Each teammate (once, ~2 minutes)

```bash
git clone https://github.com/samayc0616/claude-slack ~/claude-slack
cd ~/claude-slack && uv sync
export CLAUDE_SLACK_ROUTER_URL=ws://your-router-host:31415/v1/connect
uv run claude-slack mirror
```

The first run drops you into a polished setup TUI:

```
╭─ claude-slack mirror · first-time setup ─────────────────────╮
│  Router: ws://strata6:31415/v1/connect                        │
╰──────────────────────────────────────────────────────────────╯

── Step 1 of 2  get your API key from Slack ──
  In any Slack channel or DM, type:
    /claude register

  The Claude Code Companion bot will DM you a key that starts with cs_…
  Also: a pinned message in that DM explains how to use the bot.

── Step 2 of 2  paste the key here ──
  API key (cs_...): _
```

Switch to Slack, type `/claude register`, the bot DMs your key + pins a usage guide to that DM. Paste the `cs_…` value back into the terminal. Done forever — config is saved to `~/.config/claude-slack/config.toml`.

Subsequent runs: just `claude-slack mirror` (or `alias claude='claude-slack mirror'`).

### Same machine as the router (e.g. strata users)

It's totally fine for users to run their shim on the same machine as the router (e.g. multiple strata users on strata6 itself). The configs don't collide:
- Router config: `~/.config/claude-slack-router/`
- User shim config: `~/.config/claude-slack/`

Each user has their own home directory and therefore their own config. They use the same router URL as anyone else (`ws://strata6:31415/v1/connect`) — the connection just stays on the local loopback.

## Daily use

```bash
alias claude='claude-slack mirror'   # once, in .zshrc / .bashrc
claude                                # same UX as before, now mirrors
```

In Slack, they DM the `@claude` bot. Every assistant message, tool call, and their own typed prompts appear in the thread. Send a Slack message → it's injected into the running `claude` session as if typed.

| From the terminal | From Slack |
|---|---|
| Type as usual | All output streams into your DM with @claude |
| Hit Ctrl-C | React `:no_entry:` on any bot message → SIGINT to claude |
| `claude --resume <id>` | Resume happens through the real binary; mirror just follows along |

## Slash commands (each user)

| Command | What it does |
|---|---|
| `/claude register` | Generates a new API key, DMs it to you, also pins a usage guide in that DM. Rotates any prior key. |
| `/claude revoke` | Removes your key + disconnects any active shim |
| `/claude status` | Shows whether your shim is connected, key age, queued events |

### What gets pinned in your DM with the bot

The first time you `/claude register`, the bot pins a permanent usage guide to your DM. It covers:

- The exact command to run on your workstation
- How each `claude-slack mirror` run shows up (one thread per session)
- How to remote-control: reply in threads to inject prompts, `:no_entry:` to SIGINT
- All slash commands
- Troubleshooting (status checks, env-var verification, key rotation)

This guide is pinned so you can always scroll up to find it. The bot also greets unregistered users who DM it with a quick onboarding hint.

## Privacy model

- **Bot is shared, DMs are not.** One `@claude` in the workspace. But Alice's DM with the bot is invisible to Bob and vice versa (Slack-enforced DM privacy).
- **Cross-user prompts are blocked.** If Bob tries to message Alice's instance somehow, the router's channel-scoping refuses it. The shim also filters incoming messages by sender.
- **Bot token never leaves the router.** Users' shims proxy Slack API calls through the WebSocket — they cannot exfiltrate the token even if compromised.
- **The router host (your machine) sees prompts in flight.** This is the explicit trust call — same trust level as your internal Slack workspace itself.
- **Claude inference happens on each user's machine.** The Claude SDK talks directly to Anthropic from there.
- **Secrets are scrubbed** before mirroring: `sk-ant-*`, `xox?-*`, `ghp_*`, AWS access keys, PEM private keys, generic `api_key=` patterns.

## Admin commands

```bash
claude-slack-router list-users          # who's registered, when, last-seen
claude-slack-router add-user --slack-user U123ABC --name samay   # manual add (rare; /claude register is preferred)
claude-slack-router revoke --slack-user U123ABC                  # kick a user
```

`/claude register` from Slack is the preferred self-serve path. The CLI commands are escape hatches.

## Limitations

- **TUI rendering loss**: spinners and cursor-rewrite progress lines collapse in the Slack mirror. Text content survives.
- **Two-source input conflict**: keyboard and Slack typing simultaneously can interleave on stdin. In practice you're at one or the other.
- **PTY-bound**: the shim only works in a real terminal. No CI use.
- **Router single-point-of-failure**: if your machine is off, nobody can use the bot. Plan to move to a shared box once you outgrow that.
- **No router HA**: one router process per Slack app. If you restart it, in-flight shim WSes disconnect (they reconnect, but a turn in progress may glitch).

## What's intentionally NOT in scope

- Spawning new sessions from Slack (you start a session by running `claude-slack mirror` locally)
- Replacing or wrapping Claude Code's UI — the real `claude` is what runs, so Anthropic's updates ship to you on their normal schedule
- ANSI eye candy in Slack — we strip what doesn't render in markdown

## License

MIT.
