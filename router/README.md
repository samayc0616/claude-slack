# claude-slack: router mode

One Slack app installed once at the workspace level, used by everyone on the team. Each user's experience is a private 1:1 conversation with the bot — same model as a notifier app. No public channels, no shared threads, no cross-user visibility.

This subdirectory holds the **router** process. The user-side daemon lives in `../claude_slack/`. An admin runs `claude-slack-router` on a shared box; teammates run `claude-slack run --mode=client`.

## The model

Think of the bot like a notifier app you DM. Every Slack user gets their own private DM conversation with `@claude`. That DM is theirs and theirs alone — Slack itself enforces this, the same way it enforces that Alice can't see Bob's DMs with anyone.

```
┌─────────────────────────────────────┐
│ Slack workspace                     │
│                                     │
│  Alice ⇄ @claude  (private DM)      │  ← Alice only
│  Bob   ⇄ @claude  (private DM)      │  ← Bob only
│  Sam   ⇄ @claude  (private DM)      │  ← Sam only
│  ...                                │
└─────────────────────────────────────┘
              │ events for all DMs come into
              │ ONE socket-mode connection
              ▼
     ┌───────────────────┐
     │      router       │ ← one process, one bot token, one Slack app install
     │  (this repo)      │
     └─────┬───────┬─────┘
   outbound│       │outbound
        ws │       │ ws
           ▼       ▼
    ┌──────────┐ ┌──────────┐
    │  Alice's │ │  Bob's   │  ← each user runs their own claude-slack daemon
    │  daemon  │ │  daemon  │    locally (--mode=client). Sessions, prompts,
    │  Claude  │ │  Claude  │    and tool calls execute on the user's own
    │  local   │ │  local   │    machine.
    └──────────┘ └──────────┘
```

## What's visible to whom

| Surface | Visible to | What's there |
|---|---|---|
| Alice's DM with @claude | Alice only (Slack-enforced) | Alice's sessions, prompts, Claude responses |
| Bob's DM with @claude | Bob only | Bob's sessions, prompts, Claude responses |
| Public channel `@claude` mention | Channel members | An ephemeral "*let's continue in our DM* :inbox_tray:" only the mentioner sees, plus the mention itself which is just the trigger. No session content lives here. |
| App Home tab | Each viewer sees only their own dashboard | List of that user's sessions, cost, status |
| Assistant container (right rail) | Each user sees only their own | Same as DM, just a different surface |

No public channel ever holds prompt content. No thread is shared. Cross-user contamination is impossible at the Slack-layer, not just at the bot-layer.

## What enters and leaves Slack

- Event flow: **Slack → router → user's daemon** (over outbound WebSocket from the daemon to the router)
- Reply flow: **daemon → router → Slack** (Slack API calls proxied through the same WebSocket; the bot token never leaves the router)
- Claude inference: happens on the daemon's machine. Claude SDK still talks directly to Anthropic from the user's machine.

The router host sees plaintext prompts in flight (the trust model we agreed on). This is the same trust level as your internal Slack workspace itself.

## Wire protocol

JSON over TLS-terminated WebSocket. Endpoint: `wss://router.internal/v1/connect`.

### Client → Server

| `type` | Body | When |
|---|---|---|
| `hello` | `{api_key, daemon_version}` | First frame after connect |
| `api_call` | `{request_id, method, params}` | Daemon wants to call a Slack web API method |
| `turn_complete` | `{thread_ts, cost_usd, num_turns}` | After a Claude turn finishes, for audit |
| `ping` | `{}` | Liveness |

### Server → Client

| `type` | Body | When |
|---|---|---|
| `welcome` | `{slack_user_id, bot_user_id, bot_name, dm_channel_id}` | Auth succeeded |
| `auth_error` | `{reason}` | Auth failed, WS closes |
| `event` | `{payload}` | A Slack event for this user (`message.im`, `app_home_opened`, etc.) |
| `api_response` | `{request_id, ok, response, error?}` | Reply to a prior api_call |
| `pong` | `{}` | Reply to ping |

## Routing rules

Since sessions live in DMs only, routing is straightforward:

1. **`message.im`** → look up `channel_id`, find the user via `users.toml` reverse map → route to that daemon. (One DM channel always has exactly one human user on the other side of the bot.)
2. **`app_home_opened`** → opener's daemon
3. **`assistant_thread_started` / `assistant_thread_context_changed`** → user's daemon (assistant container is per-user)
4. **`/claude` slash command** → invoking user's daemon, regardless of where they typed it
5. **`app_mention` in any channel** → invoking user's daemon, which responds by opening / continuing their DM. The channel sees only an ephemeral redirect.
6. **`view_submission` / shortcuts** → submitting user's daemon
7. **No matching daemon (user offline)** → enqueue per-user with TTL, redeliver on reconnect. For synchronous responses (slash commands), router replies ephemerally "your claude bridge is offline."

## Provisioning

### Admin setup (once)

```bash
git clone https://github.com/samayc0616/claude-slack /opt/claude-slack
cd /opt/claude-slack
uv sync
uv run claude-slack-router init
```

The init wizard:
1. Walks through Slack app creation from a manifest (same OSC-52 + step-by-step UX as `claude-slack init`)
2. Validates the bot + app tokens
3. Writes `/etc/claude-slack-router/config.toml` (mode 0600)
4. Prints the router URL teammates will use
5. Sets up a systemd unit (optional) for auto-restart

### Onboarding a teammate

```bash
uv run claude-slack-router add-user --slack-user U123ABC --name samay
```

Prints something like:

```toml
# Send this to @samay over Slack DM:

[router]
url = "wss://router.internal.example.com/v1/connect"
api_key = "cs_a1b2c3d4e5f6..."
```

Samay drops it into `~/.config/claude-slack/config.toml` and runs:

```bash
uv run claude-slack run --mode=client
```

The daemon dials the router, presents the API key, and starts receiving events for samay's DMs only.

### Revoking access

```bash
uv run claude-slack-router revoke --slack-user U123ABC
```

Closes any active daemon WS for that user and removes the key from `users.toml`. Future connect attempts with that key are refused.

## Security model

| What we trust | Where it lives | Why it's OK |
|---|---|---|
| Router host machine | One admin-controlled box on internal network | Same trust level as the workspace's own Slack-internal services. Plaintext prompts transit it; that's the explicit trade-off for shared infrastructure. |
| Bot token (`xoxb-`) | Router only | Daemons never see it. A compromised daemon cannot exfiltrate the workspace token. |
| Per-user API keys | `users.toml` hashed at rest (sha256); user's daemon config in plaintext | Long random secret. Rotatable. A leaked key impersonates at most one user. |
| Slack TLS | n/a | Standard. |
| User's daemon machine | Their own laptop | Same trust as `claude-slack` itself: their machine runs their Claude sessions, like any local dev tool. |

### What the router enforces

- **Channel scoping**: an `api_call` from Alice's daemon to `chat.postMessage` is rewritten to fail if the target channel is not Alice's DM with the bot or a channel Alice is a member of. Alice's daemon cannot DM Bob via the bot.
- **User_id pinning**: any `user` field in api_call params is overridden to Alice's slack_user_id. Daemons cannot impersonate other users.
- **Permission gates per method**: the router whitelist names exactly the methods daemons may call (`chat.postMessage`, `chat.update`, `chat.postEphemeral`, `files.upload`, `views.publish`, `views.open`, `views.update`, `reactions.add`, `reactions.remove`, `pins.add`, `bookmarks.add`, `conversations.replies`, `assistant.threads.setStatus`, `assistant.threads.setSuggestedPrompts`, `chat.getPermalink`). Anything else returns `error: method_not_allowed`.

### Audit log

`/var/log/claude-slack-router/audit.jsonl` — one line per event:

```json
{"ts": 1715000000, "user": "samay", "event": "message.im", "channel": "D456", "routed": true, "cost_usd": 0.0123}
```

Admins can `tail -f` this for live visibility. No prompt content is logged.

## Build phases

| Phase | Scope | LOC | Status |
|---|---|---|---|
| 1 | Router: socket mode, WS server, single-user routing, basic api_call proxy (chat.postMessage / chat.update) | ~300 | not started |
| 2 | Multi-user: users.toml, add-user / revoke / list-users CLI, hashed key storage, channel scoping enforcement | ~150 | not started |
| 3 | Full api_call whitelist with per-method param validation | ~100 | not started |
| 4 | Offline-user event queue with TTL + redelivery on reconnect | ~100 | not started |
| 5 | Audit log + admin status TUI | ~80 | not started |
| daemon | Add `--mode=client` to claude-slack: ProxiedWebClient, outbound WS, reconnect, fallback to direct mode for solo users | ~200 | not started |

v1 = phases 1+2+3 plus the daemon client mode (~750 LOC total).

