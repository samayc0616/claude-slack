# claude-slack

Claude Code's Remote Control feature, rebuilt with Slack as the transport. Run `claude` in your terminal as normal — every assistant message, tool call, and your prompts get mirrored into a private Slack DM with the bot. Type in Slack from your phone or another machine; that text becomes claude's next prompt as if you'd typed it yourself.

**One app per person. Each user has their own Slack app install, their own tokens, and their own local `claude` session. No central infrastructure. No router. No shared bot to maintain.**

## How it works

```
       your keyboard ───┐                  ┌─── your terminal (verbatim)
                        ▼                  │
                 ┌──────────────────────────────┐
                 │  claude-slack mirror (shim)  │
                 │  spawns the real `claude`    │
                 │  in a PTY and forwards I/O   │
                 └─────────┬────────────────────┘
                           │
                  ┌────────▼─────────┐
                  │   real claude    │  ← the actual Anthropic binary,
                  │   binary (any    │     updates via your normal channel
                  │   version)       │
                  └────────┬─────────┘
                           │
                  ANSI-stripped output
                           │
                           ▼
                   private DM thread
                   between you and
                   YOUR @claude bot
                           ▲
                           │
                  Slack messages are
                  injected into claude's
                  stdin as if typed
```

## Setup (about 5 minutes, per user)

```bash
git clone https://github.com/samayc0616/claude-slack ~/claude-slack
cd ~/claude-slack
uv sync
uv run claude-slack init       # wizard walks you through Slack app creation
```

The wizard:
1. Generates a Slack app manifest, OSC-52 copies it to your clipboard
2. Walks you click-by-click through creating your personal app at <https://api.slack.com/apps>
3. Asks for `xoxb-` Bot Token and `xapp-` App-Level Token with `connections:write`
4. Validates the connection
5. Writes `~/.config/claude-slack/config.toml` (mode 0600)

Your workspace admin pre-approves the manifest once. After that, anyone in the workspace creates their own personal install of the same app — each gets their own bot identity, their own DM, their own everything. No cross-user visibility ever.

## Daily use

```bash
uv run claude-slack mirror     # instead of `claude`
# or, optionally:
alias claude='uv run claude-slack mirror'
```

That's it. Your terminal session looks and feels exactly like running `claude`. In parallel, a private DM with YOUR @claude bot fills up with everything that scrolls past. Send a message in the DM and it lands as claude's next prompt.

### From your phone

Open the Slack app, find your DM with your @claude, type. The text gets injected into your terminal session as if you typed it. Pull out the laptop later and your terminal has caught up.

### Interrupting

- React `:no_entry:` on any bot message in the DM → sends Ctrl-C to the underlying `claude`
- Locally, just hit Ctrl-C in the terminal as usual

### Watching from a second machine

The Slack DM is the mirror. Open Slack on any device — laptop, phone, web — and you see what's happening in your terminal in near real-time.

## Privacy model

- Each user has their own Slack app install, their own bot identity, their own DM. Slack enforces that no one else can see your DM.
- Cross-user contamination is impossible by construction: there is no shared infrastructure, just N independent installations.
- The real `claude` binary runs on your machine. Claude SDK still talks directly to Anthropic from your machine.
- Tokens (`xoxb-`, `xapp-`) live in `~/.config/claude-slack/config.toml` (mode 0600) on your machine only.
- Secrets get scrubbed before being mirrored to Slack: `sk-ant-*`, `xox?-*`, `ghp_*`, AWS access keys, generic `api_key=` patterns, PEM-encoded private keys.

## What's intentionally NOT in scope

The shim is just a mirror. It does not:

- Spawn new sessions on its own (you start a session by running `claude-slack mirror`)
- Try to be smarter than Claude Code about plans, agents, MCP, hooks — those are CLI features and they work because the real `claude` is still the thing running
- Display ANSI eye candy (spinners, cursor magic) in Slack — we strip those because they don't render in markdown. The substance gets through.

## Limitations

- **TUI rendering loss**: Claude's status spinners, cursor-rewrite progress lines, and color formatting are stripped for Slack. The text content survives; the in-place updates don't.
- **Two-source input conflict**: if you type at the keyboard and from Slack simultaneously, both reach claude's stdin and may interleave oddly. In practice you're at one or the other.
- **PTY-bound**: the shim only works in a real terminal. CI / batch contexts where you don't have a TTY can't use this.

## Admin guidance

If you're rolling this out to a team:

1. Build your own personal copy first (the steps above)
2. Get your workspace admin to **pre-approve the app manifest** so anyone with the manifest can self-install
3. Share the repo URL with teammates. Each runs `claude-slack init` and creates their own personal copy of the app

The manifest at `~/.cache/claude-slack/manifest.json` (written during your wizard run) is the canonical artifact. Share that or this repo URL with teammates so they get the identical manifest.

## Optional: legacy daemon mode

There's also `claude-slack run` which runs a daemon that spawns sessions from Slack `@mentions` (the opposite paradigm — Slack is the source of truth). Less polished, kept around for "I'm not at my workstation but want to start something" cases. See `claude_slack/daemon.py`. Mirror mode is the primary path.

## License

MIT.
