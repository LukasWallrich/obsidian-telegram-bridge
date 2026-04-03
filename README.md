# Obsidian Telegram Resource Bridge

> **Warning:** This project is vibe-coded with [Claude Code](https://claude.ai/claude-code) and has no test suite. It works well for my personal workflow, but use it with care — review the code before trusting it with your vault.

## Why?

I save articles, papers, and ideas throughout the day but rarely revisit them. This bridge closes the loop: send a link or voice note to Telegram, and a structured Obsidian note appears — summarised, tagged, and linked to related notes already in the vault. Scheduled reflection prompts and spaced-repetition revisits turn passive capture into active knowledge building.

## What It Does

```
You (Telegram)
     ↓
Telegram Bot API  ← stores updates for up to 24h while Mac is offline
     ↓ (polls every 2 min via launchd)
local/poller.py
  1. Fetches updates, groups messages into sessions (>5 min gap = new session)
  2. Waits 90s for composing, or /save to process immediately
  3. Downloads media (voice→Whisper, PDF→text, image→/tmp)
  4. Fetches URLs via Jina Reader (YouTube transcripts when available)
  5. Finds semantically related notes via Smart Connections embeddings
  6. Pipes prompt to Claude CLI → structured Markdown note
  7. Saves to Obsidian vault with frontmatter, tags, and [[wikilinks]]
  8. Optionally renders to GitHub Pages for mobile reading
  9. Replies on Telegram with confirmation + related note count
```

---

## Features

### Save Resources

Send content to Telegram and get a structured Obsidian note:

| Input | What happens |
|-------|-------------|
| **URL** | Fetched via Jina Reader, summarised by Claude |
| **PDF** | Text extracted, original saved to `Attachments/`, embedded in note |
| **Image** | Saved to `Attachments/`, described by Claude vision (add "transcribe" to caption for full text extraction) |
| **Voice note** | Transcribed by Whisper, used as context for summarisation |
| **Text document** (.md, .txt) | Content extracted, saved to `Attachments/` |
| **Plain text** | Used as context alongside other messages |
| **Combinations** | URL + voice note = summary + personal context |

Every note automatically includes semantically related notes from the vault as [[wikilinks]].

### Bot Commands

| Command | Effect |
|---------|--------|
| `/save` | Process current session immediately |
| `/clear` | Discard current session |
| `/find <query>` | Search vault notes by semantic similarity |
| `/ask <question>` | RAG query across vault notes — synthesised answer with [[citations]] |
| `/revisit [topic]` | Revisit a saved note with guided spaced-repetition reflection |
| `/reflect` | Manage reflection chains (`list`, `trigger`, `pause`, `resume`, `schedule`, `add`, `remove`) |
| `/help` | Show command list |

### Todo Inbox

Send a message starting with `todo:`, `to do:`, `task:`, or `reminder:` to append a task directly to your todo inbox — no Claude invocation, no note created. Voice notes with these openers work too.

### Spaced-Repetition Revisits

`/revisit` selects a note using spaced-repetition scheduling (intervals: 7d → 21d → 60d → 180d → 365d), shows its key takeaways alongside semantically related notes, and asks for your current thoughts. Your reply is saved as a reflection note. Also runs automatically on Tuesday and Thursday mornings.

### Scheduled Reflection Chains

Prompts sent via Telegram on a schedule to encourage regular reflection. Replies are lightly edited by Claude Haiku (preserving your voice) and saved to `Reflections/`.

| Chain | Schedule | Purpose |
|-------|----------|---------|
| Weekly Wins | Friday 18:00 | Celebrate wins, referencing week-ahead goals |
| Recording Takeaways | Saturday 09:00 | Reflect on resources saved since last Saturday |
| Week Ahead Planning | Sunday 18:00 | Set priorities for the coming week |
| Note Revisit | Tue & Thu 10:00 | Spaced-repetition note revisiting |

Manage via `/reflect list`, `/reflect pause <id>`, `/reflect trigger <id>`, etc.

### GitHub Pages (Optional)

Each saved note is rendered as a standalone HTML page with UUID-based URLs, deployed to a private GitHub repo. Mobile-friendly reading with no public listing.

---

## File Structure

```
claude_knowledge/
├── local/
│   ├── poller.py           # Main loop: poll, group sessions, invoke Claude
│   ├── media.py            # Voice (Whisper), URL (Jina), PDF, image handlers
│   ├── pages.py            # Render notes to HTML, publish to GitHub Pages
│   ├── reflections.py      # Scheduled reflection chains (config, send, process)
│   ├── revisit.py          # Spaced-repetition note revisiting (/revisit command)
│   ├── search.py           # Semantic search using Smart Connections embeddings
│   ├── config.py           # Pydantic settings from .env
│   └── state.json          # Persisted offset — auto-created, gitignored
├── reflections/
│   └── config.json         # Reflection chain definitions (schedules, prompts)
├── prompts/
│   ├── note_prompt.md      # Claude CLI prompt template for note creation
│   ├── ask_prompt.md       # Claude CLI prompt template for /ask RAG queries
│   └── reflections/        # Prompt templates for reflection chains
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

### 7. GitHub Pages (optional)

Renders each saved note as a standalone HTML page with UUID-based URLs, deployed via GitHub Actions to a private repo.

1. **Create a private GitHub repo** (e.g. `knowledge-pages`):
   ```bash
   gh repo create knowledge-pages --private --clone ~/Sites/knowledge-pages
   cd ~/Sites/knowledge-pages
   git checkout -b main 2>/dev/null
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

Once configured, each new note is automatically rendered, committed, and pushed. The Telegram reply includes the page URL plus a short preview.

**Security:** The repo is private. Pages are publicly accessible but URLs are 12-character random hex slugs with no index or listing page, plus `<meta name="robots" content="noindex, nofollow">` on every page.

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

## Security

`TELEGRAM_ALLOWED_USER_ID` in `.env` ensures only your messages are processed. All other senders are silently ignored.

## Troubleshooting

**`ModuleNotFoundError`**: Run with `.venv/bin/python local/poller.py` or activate the venv first.

**Lock file stuck after crash**: `rm local/poller.lock`

**`SAVED:` line missing from Claude output**: The poller handles this gracefully — it sends the LLM response to Telegram and advances the offset. Check `OBSIDIAN_VAULT_PATH` exists and `OBSIDIAN_RESOURCE_FOLDER` subfolder is writable.

**No updates found**: Verify you're messaging the right bot (`/getMe` returns the bot username). Updates older than 24h are dropped by Telegram.

---

## Potential Extensions

- **Duplicate detection** — `source_id` (SHA-256) is already in frontmatter; add a pre-check before creating a new note
- **Reading queue** — `/next` and `/queue` commands for notes tagged `reading-list`
- **Voice summaries** — TTS-based audio briefs of recent notes for on-the-go listening
