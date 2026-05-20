# claude-slack

Drive your local Claude Code sessions from Slack. One Slack thread = one Claude session, with full `--resume` continuity. Built to sidestep org-disabled Claude Code Remote Control.

The bridge runs locally on your machine. Your terminal still does the work; Slack is just the steering wheel — and your phone, because Slack apps notify.

## Setup (about 5 minutes)

```bash
git clone https://github.com/samayc0616/claude-slack ~/claude-slack
cd ~/claude-slack
uv sync
uv run claude-slack init   # wizard walks you through Slack app creation
uv run claude-slack run    # foreground daemon; tmux/nohup/systemd for background
```

The wizard copies a Slack app manifest into your clipboard via OSC 52 (works over SSH and tmux), then walks you click-by-click through:

1. Creating the Slack app from the manifest at <https://api.slack.com/apps>
2. Installing the app to your workspace
3. Copying the Bot OAuth Token (`xoxb-…`)
4. Generating an App-Level Token with `connections:write` (`xapp-…`)
5. Inviting the bot to a channel and picking a default

Tokens are validated with `auth.test` before anything is written. Config lands at `~/.config/claude-slack/config.toml` (mode 0600).

When the daemon starts for the first time, it posts a usage cheat-sheet to your default channel and **pins it**. So this README is just for context — the day-to-day instructions live in Slack.

## Usage

### Start a session

| You do | Result |
|---|---|
| `@claude <prompt>` in any channel the bot is in | Bot replies, opening a thread. That thread now hosts the session. |
| `/claude new <prompt>` | Same thing via slash command. |
| DM the bot directly | First message starts a session in the DM. |

### Continue a session

Reply in the thread. No `@claude` needed. Each thread maps to exactly one Claude session, with full history preserved across daemon restarts.

### While Claude is working

| You do | Result |
|---|---|
| Send another message in the thread | Bot reacts `:eyes:`, queues it, and auto-feeds it as a follow-up turn |
| Drop a file into the thread | Staged to `/tmp/claude-slack/<thread_ts>/`; the path is appended to your prompt |
| React `:no_entry:` on any bot message | Kills the session, sets status `killed` |
| React `:repeat:` | Replays your last prompt |
| Click **Interrupt** on the session card | Sends Ctrl-C to the SDK client |
| Click **Resend last** on the session card | Replays your last prompt |

### When Claude needs your input

- Status flips to `:raised_hand:` on the thread root
- You get a DM with a permalink back to the thread, so your phone wakes up
- `AskUserQuestion` shows radio (single) or checkbox (multi) blocks per question, with **Submit** and **Cancel** buttons
- `ExitPlanMode` shows the proposed plan with **Approve** / **Reject** buttons
- `SendUserFile` uploads the file to the thread with a caption

### Slash commands

| Command | What it does |
|---|---|
| `/claude list` | Lists all known sessions with status, cost, cwd |
| `/claude new <prompt>` | Starts a session in a new thread |
| `/claude kill <thread_ts>` | Force-stops a session |

### Session card

Pinned at the top of every thread. Shows session id, cwd, status, total cost, and your label. Updates in place as the session progresses. Has **Interrupt** and **Resend last** buttons.

### Status emoji on the thread root

| Emoji | Meaning |
|---|---|
| `:hourglass_flowing_sand:` | Claude is generating |
| `:raised_hand:` | Claude is waiting for you (AskUserQuestion / ExitPlanMode) |
| `:white_check_mark:` | Turn finished |
| `:x:` | Bridge errored |
| `:no_entry:` | Session killed |

## Features

- **App Home dashboard** — click the bot in Slack's left rail. See every session with status, cost, last-active, and **Jump** / **Kill** buttons. **Start new session** opens a modal with cwd + prompt + model.
- **Slack AI Apps panel** — if your workspace has Agents & AI Apps enabled, the bot lives in the right-rail assistant panel with native status (`Claude is thinking…`), suggested follow-up prompts after each turn, and seed prompts when you open it
- **Message shortcut "Send to Claude"** — right-click any message → forwards it as a new session's prompt
- **Global shortcut "Start Claude session"** — keyboard shortcut from anywhere in Slack opens the new-session modal
- **Modals for richer input** — `/claude new` (no args) and the shortcuts open a modal with cwd picker, multi-line prompt, model dropdown. **Reject** on a plan opens a feedback modal so Claude knows *why*.
- **Ephemeral "queued" notices** — only visible to you, not channel readers
- **Thread context preload** — `@claude` in an existing thread reads the prior messages and feeds them as Claude's starting context
- **DM welcome** — first DM with the bot greets you with Start Session / List Sessions buttons
- **Channel bookmark** — bot auto-adds a docs link to the channel header on startup
- **`:clipboard:` reaction** — exports the full thread transcript as a markdown snippet
- **Persistent sessions across daemon restarts** — `~/.local/state/claude-slack/sessions.json`
- **YOLO permissions** by default — every tool call auto-approves. Toggle in `~/.config/claude-slack/config.toml` if you want per-call approvals.
- **Secret redaction** — `sk-ant-*`, `xox?-*`, `ghp_*`, AWS access keys, `api_key=`, PEM keys get scrubbed before posting
- **LLM-generated thread titles** — instant first-line label, then a background `query` task summarizes it as a 4–6 word title and updates the card
- **DM-when-waiting** — phone notifications even if the thread is buried
- **`@bridge` mid-stream queue** — messages during a running turn auto-feed as the follow-up turn

## Architecture

```
your terminal                                Slack
     │                                         ▲
     │ stdout/stdin                            │ Bolt async + socket mode
     ▼                                         │
  daemon ──┬── ClaudeSDKClient (per thread) ───┘
           ├── SessionManager (JSON persist)
           ├── interactive UI mappers (Block Kit)
           └── inbound file staging
```

| File | Purpose |
|---|---|
| `claude_slack/__main__.py` | CLI dispatch: `init` / `run` / `list` / `kill` |
| `claude_slack/wizard.py` | Questionary setup flow with OSC-52 manifest copy |
| `claude_slack/clipboard.py` | OSC 52 emit (tmux + screen pass-through wrap) |
| `claude_slack/config.py` | `~/.config/claude-slack/config.toml` load/save |
| `claude_slack/sessions.py` | Per-thread state, async locks, JSON persistence |
| `claude_slack/claude_proc.py` | `ClaudeSDKClient` wrapper, streaming, `can_use_tool` routing |
| `claude_slack/daemon.py` | Slack socket-mode app, all event handlers |
| `claude_slack/slack_render.py` | ANSI strip, mrkdwn, chunking, snippet thresholds |
| `claude_slack/interactive.py` | Block Kit renderers + session card |
| `claude_slack/redact.py` | Secret scrubbing |

## Caveats

- The bot doesn't know about Claude Code sessions you started elsewhere (terminal, other machines). Every Slack thread starts a fresh session. `/claude attach <session_id>` to import an existing session is on the roadmap.
- `ExitPlanMode` semantics under `bypassPermissions` may differ from the interactive CLI; if plan-mode entries don't surface, set `yolo_permissions = false` in config.
- File uploads use `files_upload_v2`; long debugging sessions with frequent file dumps can hit Slack's per-channel rate limit.
- If the socket disconnects while a tool prompt was awaiting your click, the session will hang in `waiting`. Restart the daemon to recover.
- Secret redaction is regex-based and conservative. Inspect anything before pasting elsewhere.

## License

MIT, do whatever.
