# Obsidian Telegram Resource Bridge

Send a URL, PDF, image, or voice note to a Telegram bot. A structured Markdown note appears in your Obsidian vault — summarised by Claude Code CLI, with wikilinks to related notes found in the vault.

## Status

**Working end-to-end.** Tested 2026-02-27:

- URL sent via Telegram → Jina fetch → Claude summarises → note written to vault with correct frontmatter, dynamic tags, and wikilinks to related notes
- PDF sent via Telegram → text extracted + PDF saved to `Attachments/` → note written with embedded PDF viewer
- Text documents (.md, .txt, etc.) → content extracted (with binary validation), saved to `Attachments/` with the note title as filename
- Voice note → Whisper transcription → used as context for summarisation
- Image sent via Telegram → saved to `Attachments/` + described by Claude vision; caption passed as context; full transcription only if explicitly requested
- `/save` command triggers immediate processing without waiting for session timeout
- Nested Claude Code session issue resolved (strips `CLAUDECODE` env var before subprocess)
- Non-interactive mode: Claude always produces a note, never asks clarifying questions

**Not yet tested:**
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
  2. Handle /save, /clear, /find, /ask, /help commands
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
claude_knowledge/
├── local/
│   ├── poller.py           # Main loop: poll, group sessions, invoke Claude
│   ├── media.py            # Voice (Whisper), URL (Jina), PDF, image handlers
│   ├── pages.py            # Render notes to HTML, publish to GitHub Pages
│   ├── search.py           # Semantic search using Smart Connections embeddings
│   ├── config.py           # Pydantic settings from .env
│   └── state.json          # Persisted offset — auto-created, gitignored
├── prompts/
│   ├── note_prompt.md      # Claude CLI prompt template for note creation
│   └── ask_prompt.md       # Claude CLI prompt template for /ask RAG queries
├── templates/
│   └── note.html           # Jinja2 template for rendered HTML notes
├── scripts/
│   └── backfill_pages.py   # One-off: render all existing notes to Pages
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

### 5. Smart Connections (required for semantic search)

Install the [Smart Connections](https://github.com/brianpetro/obsidian-smart-connections) Obsidian plugin and let it build embeddings. The bridge uses its `TaylorAI/bge-micro-v2` embeddings for the `/find` command and for linking related notes during note creation. The ONNX model (~20 MB) is downloaded automatically on first use.

### 6. Test manually

```bash
source .venv/bin/activate
python local/poller.py
```

Send a URL to your bot. After 90 seconds (or after sending `/save`), run again and check your vault.

### 7. GitHub Pages (optional — mobile-friendly note reading)

Renders each saved note as a standalone HTML page with UUID-based URLs, deployed via GitHub Actions to a private repo.

1. **Create a private GitHub repo** (e.g. `knowledge-pages`):
   ```bash
   gh repo create knowledge-pages --private --clone ~/Sites/knowledge-pages
   cd ~/Sites/knowledge-pages
   git checkout -b main 2>/dev/null  # ensure branch is named main
   touch .nojekyll
   ```

2. **Add the GitHub Actions workflow** (`.github/workflows/deploy.yml`):
   ```bash
   mkdir -p .github/workflows
   ```
   Create `.github/workflows/deploy.yml`:
   ```yaml
   name: Deploy to GitHub Pages
   on:
     push:
       branches: [main]
   permissions:
     contents: read
     pages: write
     id-token: write
   concurrency:
     group: pages
     cancel-in-progress: false
   jobs:
     deploy:
       runs-on: ubuntu-latest
       environment:
         name: github-pages
         url: ${{ steps.deployment.outputs.page_url }}
       steps:
         - uses: actions/checkout@v4
         - uses: actions/configure-pages@v5
         - uses: actions/upload-pages-artifact@v3
           with:
             path: .
         - id: deployment
           uses: actions/deploy-pages@v4
   ```

3. **Push and enable Pages**:
   ```bash
   git add -A && git commit -m "Initial setup" && git push -u origin main
   ```
   Then enable Pages via the API (or in repo Settings > Pages > Source: GitHub Actions):
   ```bash
   gh api repos/<USER>/knowledge-pages/pages -X PUT \
     --input - <<< '{"build_type":"workflow","source":{"branch":"main","path":"/"}}'
   ```

4. **Configure the bridge** — add to `.env`:
   ```
   PAGES_REPO_PATH=/Users/you/Sites/knowledge-pages
   PAGES_BASE_URL=https://<user>.github.io/knowledge-pages
   ```

5. **Backfill existing notes** (optional):
   ```bash
   source .venv/bin/activate
   python scripts/backfill_pages.py
   ```

Once configured, each new note saved via Telegram is automatically rendered to HTML, committed, and pushed. The Telegram reply includes the page URL plus a short preview.

**Security:** The repo is private (source not visible). Pages are publicly accessible but URLs are 12-character random hex slugs with no index or listing page, plus `<meta name="robots" content="noindex, nofollow">` on every page.

### 8. Install launchd daemon

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
| `/find <query>` | Search vault notes by semantic similarity |
| `/ask <question>` | Ask a question across your vault notes (RAG) |
| `/help` | Show command list |

## Todo Inbox

Send a text message or voice note starting with any of these openers to append a task directly to your todo inbox (no Claude invocation, no note created):

| Opener | Example |
|--------|---------|
| `todo:` | `todo: email Sarah about the proposal` |
| `to do:` / `to-do:` | `to do: review chapter 3` |
| `add todo:` / `add a todo:` | `add a todo: book dentist` |
| `task:` | `task: update dependencies` |
| `reminder:` | `reminder: call back at 3pm` |

Voice notes work identically — just say one of the openers at the start. If a session contains a voice todo alongside other content (e.g. a URL), the todo is added to the inbox and the URL is saved as a note separately.

## What to Send

- **URL** — fetched via Jina Reader, summarised by Claude
- **PDF** — text extracted + original saved to `Attachments/{note title}.pdf`, embedded in note
- **Text documents** (.md, .txt, etc.) — content extracted and saved to `Attachments/{note title}.md`; files without a text extension default to `.md`
- **Voice note** — transcribed by Whisper, used as context to guide summarisation; Claude generates a "why I saved this" purpose statement. Start with a todo opener (see below) to add a task directly to your inbox instead.
- **Image** — saved to `Attachments/{note title}.jpg`, described by Claude vision (summary by default; add "transcribe" to caption to extract full text); caption passed as context
- **Plain text** — used as context alongside other messages in the session
- **Combinations** — URL + voice note in same session = summary + personal context

---

## Next Steps

### High priority
- [ ] **Install and verify launchd daemon** — run `bash install_launchd.sh`, check logs, confirm auto-polling works after reboot
- [ ] **Test offline recovery** — stop launchd, send messages, restart Mac, confirm backlog is processed (within 24h window)

### Quality improvements
- [ ] **Duplicate detection** — `source_id` (SHA-256) is already in frontmatter; add a pre-check that searches the vault for an existing note with the same `source_id` before creating a new one
- [ ] **Better session feedback** — send a Telegram acknowledgement when a session starts processing, not just when it finishes

### Phase 2 features
- [x] **`/find <query>`** — semantic search over vault using Smart Connections embeddings
- [x] **Semantic related notes** — note creation uses embedding similarity to find related notes
- [x] **GitHub Pages** — rendered HTML notes with UUID slugs, wikilinks, and dark/light mode
- [x] **YouTube support** — transcript extraction for YouTube URLs
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
