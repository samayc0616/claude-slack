# claude-slack-router

One Slack app, many local Claude daemons. The router holds the single Slack socket-mode connection on behalf of the whole team and fans events out to each user's personal daemon.

## Architecture

```
                              ┌──────────────────┐
                              │  Slack (one app, │
                              │  one xapp token) │
                              └─────────┬────────┘
                                socket mode (1)
                                        │
                              ┌─────────▼────────┐
                              │     router       │
                              │  (this process)  │
                              │  - holds bot tok │
                              │  - thread→user   │
                              │  - users.toml    │
                              └──┬───────────┬───┘
                  outbound WS ▲  │           │  ▲ outbound WS
                  reconnect-able │           │  reconnect-able
                                 │           │
                  ┌──────────────┴──┐     ┌──┴────────────────┐
                  │ samay's daemon  │     │ alice's daemon    │
                  │ (--mode=client) │     │ (--mode=client)   │
                  │ Claude on his   │     │ Claude on her     │
                  │ workstation     │     │ MacBook           │
                  └─────────────────┘     └───────────────────┘
```

**Key properties:**

1. **Single Slack app installation.** Admin approves once. The same manifest as `claude-slack/wizard.py` but installed by the router admin, not each user.
2. **Daemons run wherever the user wants.** Inbound network access to the daemon is not required — daemons open an outbound WebSocket to the router.
3. **Bot token stays on the router.** Daemons proxy all Slack API calls through the same WS. A compromised daemon cannot exfiltrate the workspace token.
4. **Thread ownership is sticky.** The user who first `@claude`s a thread owns it. All subsequent messages in that thread go to their daemon, regardless of who sent them.
5. **Per-user API keys.** Admin generates a key per Slack user, hands it out once. Daemon presents the key on connect; router verifies and binds the WS to that Slack user id.

## Wire protocol

Every WS frame is a JSON object with a `type` field. Encoding is plain JSON over a TLS-terminated WebSocket (`wss://`). Versioning lives in the URL path: `/v1/connect`.

### Client → Server

| `type` | Purpose |
|---|---|
| `hello` | First frame after connect. `{ api_key, daemon_version }`. Router replies with `welcome` or `auth_error`. |
| `api_call` | Daemon wants to call a Slack web API method. `{ request_id, method, params }`. Router returns `api_response`. |
| `ping` | Liveness. Router replies `pong`. |

### Server → Client

| `type` | Purpose |
|---|---|
| `welcome` | Auth succeeded. `{ slack_user_id, bot_user_id, bot_name }`. |
| `auth_error` | Auth failed. WS will be closed. |
| `event` | Slack event delivered to this daemon. `{ payload: <slack event dict> }`. |
| `api_response` | Reply to a prior `api_call`. `{ request_id, ok, response }`. |
| `pong` | Reply to a ping. |

### Failure modes

- **Daemon offline when event arrives.** Router enqueues per-user with TTL (24h default), redelivers on next connect. Queue depth capped at 1000 per user; overflows drop the oldest with a warning.
- **Router crashes / restarts.** Daemons see the WS close, retry with exponential backoff capped at 30 s. Sessions resume cleanly because Claude SDK session state lives on the daemon's disk, not the router's.
- **Slack reconnect.** Router uses Bolt's auto-reconnect. Events delivered during the gap are replayed by Slack on reconnect (Slack guarantees at-least-once).

## Routing rules

When a Slack event arrives at the router, decide whose daemon gets it:

1. **`app_mention`** with no parent thread → mention author's daemon. Router records `thread_ts → user_id`.
2. **`app_mention`** in an existing thread → if thread is owned, route to owner; else use mention author and record ownership.
3. **`message`** with `thread_ts`, thread owned → route to owner. The author may differ; this means non-owner contributions are visible to the owner's Claude session. (Pinned welcome warns users.)
4. **`message`** in DM with bot → DM user's daemon.
5. **`reaction_added`** on a message in a known thread → owner's daemon. Reactor is included in the payload.
6. **`/claude` slash command** → invoking user's daemon. (Slash payloads carry `user_id` natively.)
7. **`app_home_opened`** → opener's daemon. (Each user sees only their own dashboard.)
8. **Modal `view_submission`** / shortcut → submitter's daemon. Trigger_id was issued to that user's interaction; only their daemon handled the opening views.open.
9. **No matching daemon (offline or unknown user)** → enqueue if user is known; reply ephemerally "your claude bridge is offline" if it's a slash command or shortcut where a synchronous response is expected.

## Provisioning flow

### Initial setup (admin, once)

```bash
cd ~/claude-slack
uv run claude-slack-router init    # creates Slack app, prompts for tokens,
                                    # writes router config + empty users.toml,
                                    # prints a connect URL for daemons
```

The init wizard uses the same OSC-52 manifest copy + click-by-click instructions as `claude-slack init` does. The differences are:
- One-time admin-only step
- Stores tokens in `/etc/claude-slack-router/config.toml` (or `XDG_CONFIG_HOME/claude-slack-router/`)
- Sets `router_url` that users will paste into their daemon config

### Add a user (admin, per teammate)

```bash
uv run claude-slack-router add-user --slack-user U123ABC --name samay
# prints: api_key=cs_abc123...
#         daemon config snippet to send to samay
```

The admin sends the snippet to samay over Slack DM.

### User onboarding (each teammate, once)

Samay copies the snippet into `~/.config/claude-slack/config.toml`:

```toml
[router]
url = "wss://router.internal.example.com/v1/connect"
api_key = "cs_abc123..."
```

Then:

```bash
uv run claude-slack run --mode=client
```

The daemon dials the router. On first connect the router pushes a `welcome` with the bot's user id and name so the daemon can render mentions correctly. Sessions on the daemon's disk are unchanged — `claude-agent-sdk` still uses `~/.claude/projects/...` locally.

## Security model (explicit, since you asked)

| What we trust | Justification |
|---|---|
| The router host machine | One admin-controlled server. Same trust level as internal Jira, git, Slack itself. Prompts and Claude responses transit this host in plaintext over a TLS link. |
| Per-user API keys | Long random strings, stored in `users.toml` with mode 0600. Rotatable. Hashed at rest (sha256) so a database leak doesn't directly authenticate. |
| Slack TLS | Slack delivers events over TLS; we trust their PKI. |
| Daemon machines | We trust each user's machine, same as `claude-slack` itself does. A compromised daemon can leak its own user's prompts, but cannot reach other users' daemons or steal the bot token. |

### What we do **not** trust

- **Daemons calling Slack methods on behalf of other users.** Every `api_call` is rewritten by the router to pin the relevant scope (channel must be one the user has access to; user_id fields default to the daemon's bound user_id). Forging `chat_postMessage` to another user's DM is blocked.
- **Daemons posing as another user.** API key → slack_user_id binding is enforced. A leaked key only impersonates one user.

### Audit log

Router writes one structured JSONL line per event to `/var/log/claude-slack-router/audit.log`:

```json
{"ts": 1715000000, "slack_user": "U123", "event": "app_mention", "channel": "C456", "thread_ts": "...", "routed_to": "samay", "cost_usd": null}
```

After a turn finishes the daemon sends back a `turn_complete` message with the cost. Router updates the audit row.

## Build phases

| Phase | Scope | LOC | Status |
|---|---|---|---|
| 1 | Router process: socket mode, WS server, single-user routing, api_call proxy for `chat.postMessage` and `chat.update` | ~300 | not started |
| 2 | Multi-user provisioning (add-user / list-users / revoke), users.toml, thread ownership table on disk | ~150 | not started |
| 3 | Full api_call proxy: every method the daemon uses (~12 methods); permission gates per user | ~150 | not started |
| 4 | Offline-user queue with TTL + redelivery, reconnect with state resume | ~100 | not started |
| 5 | Audit log, per-user rate limits, admin TUI for live status | ~100 | not started |

v1 is phases 1+2+3. Phases 4+5 are polish.
