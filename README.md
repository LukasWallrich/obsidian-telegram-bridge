# Obsidian Telegram Resource Bridge

Send a URL, PDF, image, or voice note to a Telegram bot. A structured Markdown note appears in your Obsidian vault — summarised by Claude Code CLI, with wikilinks to related notes found in the vault.

## Status

**Working end-to-end.** Tested 2026-02-27:

- URL sent via Telegram → Jina fetch → Claude summarises → note written to vault with correct frontmatter, dynamic tags, and wikilinks to related notes
- PDF sent via Telegram → text extracted + PDF saved to `Attachments/` → note written with embedded PDF viewer
- Text documents (.md, .txt, etc.) → content extracted (with binary validation), saved to `Attachments/` with the note title as filename
- Voice note → Whisper transcription → used as context for summarisation
- `/save` command triggers immediate processing without waiting for session timeout
- Nested Claude Code session issue resolved (strips `CLAUDECODE` env var before subprocess)
- Non-interactive mode: Claude always produces a note, never asks clarifying questions

**Not yet tested:**
- Image handling (download → Claude vision)
- launchd auto-polling daemon

---

## Architecture

```
You (Telegram)
     ↓
Telegram Bot API  ← stores updates for up to 24h while Mac is offline
     ↓ (polls every 2 min via launchd when Mac is online)
local/poller.py
  1. getUpdates(offset=last_confirmed+1)
  2. Handle /save, /clear, /help commands
  3. Group messages into sessions (>5 min gap = new session)
  4. Skip sessions where last message <90s ago (still composing)
  5. For ready sessions:
       a. Download media (voice→Whisper, PDF/text→deferred, image→/tmp)
       b. Fetch URL via Jina Reader (authenticated, with fallback)
       c. Write prompt to /tmp/bridge_prompt_<id>.md
       d. Pipe to: claude --print --dangerously-skip-permissions
       e. Parse "SAVED: <filename>" from stdout
       f. Save deferred attachments to Attachments/ using note title
       g. Replace attachment placeholders in the note
       h. Advance offset, save state.json
       i. Reply on Telegram: "Saved: [[Note Title]] — N related notes linked"
```

**24-hour limitation**: Telegram drops unprocessed updates after 24h of the Mac being offline. Acceptable for typical use.

---

## File Structure

```
claude_claw/
├── local/
│   ├── poller.py           # Main loop: poll, group sessions, invoke Claude
│   ├── media.py            # Voice (Whisper), URL (Jina), PDF, image handlers
│   ├── config.py           # Pydantic settings from .env
│   └── state.json          # Persisted offset — auto-created, gitignored
├── prompts/
│   └── note_prompt.md      # Claude CLI prompt template
├── com.user.obsidian-bridge.plist  # launchd daemon (edit paths, then install)
├── install_launchd.sh      # Substitutes paths and loads the plist
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Save the bot token

### 2. Get your Telegram user ID

Send `/start` to [@userinfobot](https://t.me/userinfobot).

### 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, OPENAI_API_KEY,
#          JINA_API_KEY, OBSIDIAN_VAULT_PATH
```

### 5. Test manually

```bash
source .venv/bin/activate
python local/poller.py
```

Send a URL to your bot. After 90 seconds (or after sending `/save`), run again and check your vault.

### 6. Install launchd daemon

```bash
bash install_launchd.sh
```

Runs every 2 minutes. Logs to `~/Library/Logs/obsidian-bridge.log`.

To uninstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.obsidian-bridge.plist
rm ~/Library/LaunchAgents/com.user.obsidian-bridge.plist
```

---

## Bot Commands

| Command | Effect |
|---------|--------|
| `/save` | Process current session immediately |
| `/clear` | Discard current session |
| `/help` | Show command list |

## What to Send

- **URL** — fetched via Jina Reader, summarised by Claude
- **PDF** — text extracted + original saved to `Attachments/{note title}.pdf`, embedded in note
- **Text documents** (.md, .txt, etc.) — content extracted and saved to `Attachments/{note title}.md`; files without a text extension default to `.md`
- **Voice note** — transcribed by Whisper, used as context to guide summarisation; Claude generates a "why I saved this" purpose statement
- **Image** — saved to vault, described by Claude vision
- **Plain text** — used as context alongside other messages in the session
- **Combinations** — URL + voice note in same session = summary + personal context

---

## Next Steps

### High priority
- [ ] **Test image handling** — send a photo, verify it's saved to vault and described in the summary
- [ ] **Install and verify launchd daemon** — run `bash install_launchd.sh`, check logs, confirm auto-polling works after reboot
- [ ] **Test offline recovery** — stop launchd, send messages, restart Mac, confirm backlog is processed (within 24h window)

### Quality improvements
- [ ] **Duplicate detection** — `source_id` (SHA-256) is already in frontmatter; add a pre-check that searches the vault for an existing note with the same `source_id` before creating a new one
- [ ] **Better session feedback** — send a Telegram acknowledgement when a session starts processing, not just when it finishes

### Phase 2 features
- [ ] **`/find <query>`** — semantic search over vault, reply with top matching note titles
- [ ] **YouTube support** — `yt-dlp` transcript extraction for YouTube URLs
- [ ] **Webhook fallback** — small Cloudflare Worker to extend beyond the 24h offline limit

---

## Security

`TELEGRAM_ALLOWED_USER_ID` in `.env` ensures only your messages are processed. All other senders are silently ignored at the update-grouping stage — no reply, no processing.

## Troubleshooting

**`ModuleNotFoundError`**: Run with `.venv/bin/python local/poller.py` or activate the venv first.

**Lock file stuck after crash**: `rm local/poller.lock`

**`SAVED:` line missing from Claude output**: The poller now handles this gracefully — it sends the LLM response to Telegram and advances the offset. Check `OBSIDIAN_VAULT_PATH` exists and `OBSIDIAN_RESOURCE_FOLDER` subfolder is writable.

**No updates found**: Verify you're messaging the right bot (`/getMe` returns the bot username). Updates older than 24h are dropped by Telegram.

**Nested Claude session error**: The poller strips the `CLAUDECODE` env var before invoking Claude — this is already handled. If it recurs, check you're running the latest `poller.py`.
