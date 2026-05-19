# claude-slack

Bridge Claude Code sessions into Slack threads. One Slack thread = one Claude session, with full `--resume` continuity. Built to sidestep org-disabled Remote Control.

## Status

**v1 complete.** Untested against a live workspace. Expect to fix 1–2 small bugs on the first real run.

| Feature | Status |
|---|---|
| TUI setup wizard with manifest generator, token validation, channel picker | done |
| Socket-mode daemon, thread↔session mapping, persistent across restarts | done |
| Streaming assistant text → Slack, long output → snippet | done |
| ANSI strip, CommonMark → Slack mrkdwn | done |
| Secret redaction (sk-ant / xoxb / ghp / AKIA / api_key / private keys) | done |
| Status emoji on thread parent (running/waiting/done/error/killed) | done |
| Pinned session card with cwd / session id / cost, updated in place | done |
| Interrupt + Resend buttons on session card | done |
| `AskUserQuestion` → Block Kit radio/checkbox with Submit + Cancel, round-trip wired | done |
| `ExitPlanMode` → Approve / Reject buttons, round-trip wired | done |
| `SendUserFile` → uploads each file to the Slack thread | done |
| Slash commands: `/claude new <prompt>`, `/claude list`, `/claude kill <ts>` | done |
| Reaction shortcuts: `:no_entry:` kills, `:repeat:` resends last prompt | done |
| Inbound file staging: drop a file in the thread → bridge stages to `/tmp/claude-slack/<ts>/` and tells Claude | done |
| Auto-name threads from first prompt (first-line heuristic, instant) | done |
| LLM-summarized thread names (background `query` task, updates card when ready) | done |
| DM-when-waiting: phone-notif ping when Claude calls AskUserQuestion / ExitPlanMode | done |
| `@bridge` mid-stream injection: messages sent during a running turn are queued and auto-fed as a follow-up turn | done |

## Setup

```bash
cd ~/claude-slack
uv sync
uv run claude-slack init   # interactive wizard
uv run claude-slack run    # foreground daemon; tmux/nohup/systemd for background
```

The wizard prints a JSON Slack-app manifest. Paste it at <https://api.slack.com/apps> → **Create New App → From manifest**, install to your workspace, then copy:
- **Bot User OAuth Token** (`xoxb-...`) under OAuth & Permissions
- **App-Level Token** with `connections:write` scope under Basic Information → App-Level Tokens

Paste both into the wizard. The wizard tests `auth.test`, lists channels the bot can see, asks which one to default to.

## Slack UX

| You do | Bot does |
|---|---|
| `@claude do X` in a channel | Starts a new session; opens a thread; posts session card |
| Reply in that thread | Resumes the same session with your message as the next turn |
| Drop a file in the thread | Downloads to `/tmp/claude-slack/<thread_ts>/`, tells Claude its path |
| Click **Interrupt** on the card | Sends Ctrl-C to the SDK client |
| Click **Resend last** | Replays your last prompt (handy if Claude errored out) |
| React `:no_entry:` to any bot message | Kills the session, marks status `killed` |
| React `:repeat:` | Same as Resend last |
| Claude calls `AskUserQuestion` | You get radio/checkbox blocks per question + Submit/Cancel |
| Claude calls `ExitPlanMode` | You get the plan + Approve/Reject buttons |
| Claude calls `SendUserFile` | The file is uploaded to the thread |
| `/claude list` | Lists all known sessions with status and cost |
| `/claude new <prompt>` | Starts a session in a new thread |
| `/claude kill <thread_ts>` | Forces session shutdown |
| Reply mid-run (`@claude also check X`) | Bridge reacts `:eyes:`, queues, replies "queued for next turn", and auto-feeds it as a follow-up turn the moment Claude finishes |
| Claude blocks on a question | You get a DM ping with a link back to the thread, so your phone wakes up |

The bot has `bypassPermissions` set, so every tool call auto-approves. If you want approval-per-call, edit `~/.config/claude-slack/config.toml` and set `[features] yolo_permissions = false`.

## Files

| Path | Purpose |
|---|---|
| `claude_slack/__main__.py` | CLI dispatch: init / run / list / kill |
| `claude_slack/wizard.py` | Questionary setup flow |
| `claude_slack/config.py` | Config at `~/.config/claude-slack/config.toml` |
| `claude_slack/sessions.py` | Session registry, per-thread locks, JSON persistence |
| `claude_slack/claude_proc.py` | `ClaudeSDKClient` wrapper, streaming, can_use_tool routing |
| `claude_slack/daemon.py` | Slack socket-mode app, button/reaction/slash handlers |
| `claude_slack/slack_render.py` | ANSI strip, mrkdwn, chunking, snippet thresholds |
| `claude_slack/interactive.py` | Block Kit renderers for AskUserQuestion / ExitPlanMode / session card |
| `claude_slack/redact.py` | Secret scrubbing before posting |

## Known caveats

- `ExitPlanMode` approval path returns `PermissionResultAllow`, but the SDK's plan-mode semantics under `bypassPermissions` may not surface plan-mode entries the way they do in interactive CLI. May need to switch the session to `permission_mode='default'` for plans to take effect.
- File uploads use `files_upload_v2` which Slack rate-limits aggressively. Long debugging sessions with frequent file dumps may hit `ratelimited`.
- The Socket Mode handler is a single long-lived websocket. If it disconnects, slack-bolt auto-reconnects, but in-flight tool prompts that were awaiting Future resolution will hang. Restart the daemon if a thread sits in `waiting` forever.
- Secret redaction is regex-based and conservative. Inspect output before pasting any redacted snippet elsewhere; it may have false negatives.

## Next steps once it's running

1. Smoke-test against your workspace: one `@claude what's 2+2`, one `@claude make me pick a Python or Go option`, one `@claude show me a plan for ... then exit plan mode`.
2. Wire the SDK's `additionalContext` hook to inject `@bridge` mentions mid-stream (Tier 4).
3. Background-job watcher: link a thread to a Slurm jobid, post on state change.
