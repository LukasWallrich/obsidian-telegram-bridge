# Extension Ideas

## Smarter search (`/ask` RAG search)
- **`/ask <question>`** — Retrieve top-N related notes, feed their full text to Claude, get a synthesized answer with citations (`[[Note]]`). RAG over the vault for "what do I know about X?"
- **`/digest <topic>`** — Longer thematic summary across related notes, highlighting contradictions or evolution in thinking over time.
- Implementation is straightforward: semantic search + Claude invocation already exist. Main cost is token usage.

## Weekly reflection prompts
- Friday-only scheduled script (or branch in poller) that:
  - Gathers all notes saved that week (glob by date prefix)
  - Summarizes themes/topics captured
  - Sends Telegram message: "This week you saved 8 notes, mostly about X and Y. Any reflections?"
  - Reply saved as `Reflections/2026-W12.md`
- Could extend to monthly reviews surfacing patterns across weekly reflections.
- Key value: turns passive capture into active processing.

## Spaced resurfacing
- Daily or weekly, bot sends a previously saved note — random or weighted by age/importance.
- "You saved this 30 days ago — still relevant?" Forces re-engagement and pruning.

## Reading queue management
- Notes tagged `reading-list` get `/next` command (serves oldest unread) and `/queue` (shows pending).
- Track read/unread status in frontmatter.

## Cross-note synthesis on save
- When saving, if semantic search finds highly similar notes (cosine > 0.85), flag: "This seems closely related to [[Other Note]] — merge or link?"
- Prevents knowledge fragmentation.

## Voice-first interaction
- `/brief` command: Claude reads recent notes, generates audio summary via TTS API.
- Useful for commute listening.
