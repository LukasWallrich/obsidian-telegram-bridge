"""
poller.py — Main polling loop for the Obsidian Telegram Resource Bridge.

Flow:
  1. Acquire lock (exit immediately if another instance is running)
  2. getUpdates from Telegram (offset = last_offset + 1)
  3. Handle bot commands (/save, /clear, /help)
  4. Group remaining messages into per-sender sessions (5-min gap threshold)
  5. Skip sessions whose last message is <SESSION_TIMEOUT_SECONDS old
  6. For ready sessions: process → invoke Claude CLI → send Telegram reply
  7. Advance offset only past fully processed updates; persist to state.json
  8. Release lock
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Allow running directly from local/ or from project root
sys.path.insert(0, str(Path(__file__).parent))
from config import settings
from media import extract_pdf, extract_text_document, fetch_url, fetch_youtube_transcript, is_youtube_url, save_image, sha256_of, transcribe_voice
from pages import git_commit_and_push, publish_note
from reflections import (
    check_and_send_reflections,
    handle_reflect_command,
    match_pending_reflection,
    process_reflection_response,
)
from revisit import handle_revisit_command, record_revisit
from search import collect_vault_tags, search_vault

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
STATE_FILE = BASE_DIR / "local" / "state.json"
LOCK_FILE = BASE_DIR / "local" / "poller.lock"
PROMPT_TEMPLATE = BASE_DIR / "prompts" / "note_prompt.md"
ASK_PROMPT_TEMPLATE = BASE_DIR / "prompts" / "ask_prompt.md"

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
SESSION_GAP = 300  # seconds — gap between messages that starts a new session

# Matches voice-todo openers: "todo: …", "to do: …", "to-do: …",
# "add (a) todo: …", "task: …", "reminder: …"
_TODO_RE = re.compile(
    r"^(todo|to[\s\-]do|add (a )?to[\s\-]?do|task|reminder)[:\s]+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def acquire_lock() -> bool:
    """Take the single-instance lock, reclaiming it if the holder is dead.

    The lock records the holder's PID so an unclean exit (e.g. SIGKILL on
    shutdown) cannot leave a stale lock that blocks every future start.
    """
    if LOCK_FILE.exists():
        try:
            holder = int(LOCK_FILE.read_text().strip() or "0")
        except (ValueError, OSError):
            holder = 0
        if _pid_alive(holder):
            logger.info("Lock held by running PID %d — exiting.", holder)
            return False
        logger.warning("Reclaiming stale lock (PID %s not running).", holder or "unknown")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_offset": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def tg_get(method: str, **params) -> dict:
    r = httpx.get(f"{TELEGRAM_API}/{method}", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    return data["result"]


def tg_post(method: str, **params) -> dict:
    r = httpx.post(f"{TELEGRAM_API}/{method}", json=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    return data["result"]


def send_message(chat_id: int, text: str, reply_to_message_id: int | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        params["reply_to_message_id"] = reply_to_message_id
    return tg_post("sendMessage", **params)


def get_updates(offset: int) -> list[dict]:
    params: dict = {"timeout": 0}
    if offset > 0:
        params["offset"] = offset
    return tg_get("getUpdates", **params)


# ---------------------------------------------------------------------------
# Session grouping
# ---------------------------------------------------------------------------


def group_into_sessions(
    updates: list[dict],
    split_after_update_ids: set[int] | None = None,
) -> list[list[dict]]:
    """
    Group updates from the allowed user into sessions.
    A new session starts when:
      - there is a >SESSION_GAP second gap between messages, OR
      - the previous update_id is in split_after_update_ids (e.g. a /save command
        was between these messages, so they belong to separate sessions).
    Updates from other senders are silently dropped.
    """
    split_ids = split_after_update_ids or set()

    allowed_updates = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        sender_id = msg.get("from", {}).get("id")
        if sender_id != settings.telegram_allowed_user_id:
            logger.debug("Ignoring update from user %s", sender_id)
            continue
        allowed_updates.append(upd)

    if not allowed_updates:
        return []

    sessions: list[list[dict]] = []
    current_session: list[dict] = [allowed_updates[0]]

    for upd in allowed_updates[1:]:
        msg = upd.get("message") or upd.get("edited_message") or {}
        prev_msg = current_session[-1].get("message") or current_session[-1].get("edited_message") or {}
        gap = msg.get("date", 0) - prev_msg.get("date", 0)

        # Split on time gap OR if a /save command fell between these two updates
        prev_id = current_session[-1]["update_id"]
        cur_id = upd["update_id"]
        save_between = any(prev_id < sid < cur_id for sid in split_ids)

        if gap > SESSION_GAP or save_between:
            sessions.append(current_session)
            current_session = [upd]
        else:
            current_session.append(upd)

    sessions.append(current_session)
    return sessions


def session_last_message_age(session: list[dict]) -> float:
    """Seconds since the last message in the session was sent."""
    last_msg = session[-1].get("message") or session[-1].get("edited_message") or {}
    ts = last_msg.get("date", 0)
    return time.time() - ts


def session_max_update_id(session: list[dict]) -> int:
    return max(upd["update_id"] for upd in session)


# ---------------------------------------------------------------------------
# Todo inbox
# ---------------------------------------------------------------------------


def process_todo_message(task_text: str, chat_id: int) -> None:
    """Append a checkbox task to the todo inbox note."""
    inbox_path = Path(settings.obsidian_vault_path) / settings.obsidian_todo_inbox
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    task_line = f"- [ ] {task_text}  _(added {date_str})_\n"

    if inbox_path.exists():
        content = inbox_path.read_text()
        inbox_path.write_text(content.rstrip("\n") + "\n" + task_line)
    else:
        inbox_path.write_text(f"# Todo Inbox\n\n{task_line}")

    logger.info("Todo added to %s: %s", inbox_path, task_text[:80])
    send_message(chat_id, f"Added to todo inbox: {task_text}")


def process_image_todo(file_id: str, update_id: int, chat_id: int) -> None:
    """Download image, ask Claude to extract todo items, add them to the inbox."""
    path, _ = save_image(file_id, update_id)
    tmp_path = Path(f"/tmp/bridge_todo_img_{update_id}.md")
    vault_path = str(settings.obsidian_vault_path)
    try:
        prompt = (
            f"Use your Read tool to view the image at {path}.\n\n"
            f"Extract all to-do items, tasks, checklist items, or reminders visible in the image.\n"
            f"Return each item on a separate line starting with '- '.\n"
            f"If there are no to-do items in the image, return exactly: NO_TODOS"
        )
        tmp_path.write_text(prompt)
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        with open(tmp_path) as f:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", "--model", "claude-sonnet-4-6"],
                stdin=f,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=vault_path,
                env=env,
            )

        if result.returncode != 0:
            logger.error("Claude stderr: %s", result.stderr[:500])
            send_message(chat_id, "Failed to extract todos from image.")
            return

        todos = [line[2:].strip() for line in result.stdout.splitlines() if line.startswith("- ")]
        if not todos:
            send_message(chat_id, "No to-do items found in the image.")
            return

        for task_text in todos:
            process_todo_message(task_text, chat_id)

    finally:
        tmp_path.unlink(missing_ok=True)
        Path(f"/tmp/bridge_img_{update_id}.jpg").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------


def handle_commands(
    updates: list[dict],
    force_ready_chat_ids: set[int],
    cleared_chat_ids: set[int],
    save_update_ids: set[int],
    state: dict | None = None,
) -> list[dict]:
    """
    Process bot commands. Returns remaining (non-command) updates.
    Modifies force_ready_chat_ids, cleared_chat_ids, and save_update_ids in place.
    save_update_ids collects update_ids of /save commands so they can be used
    as session split points.
    """
    if state is None:
        state = {}
    remaining = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            remaining.append(upd)
            continue

        text = msg.get("text", "")
        chat_id = msg["chat"]["id"]
        sender_id = msg.get("from", {}).get("id")

        if sender_id != settings.telegram_allowed_user_id:
            continue  # silently drop

        # Check for reflection reply (reply-to a pending reflection prompt)
        reply_to = msg.get("reply_to_message", {})
        reply_msg_id = reply_to.get("message_id")
        if reply_msg_id:
            pending = match_pending_reflection(state, reply_msg_id)
            if pending:
                # Transcribe voice if needed, then process as reflection
                user_text = text
                if msg.get("voice"):
                    try:
                        user_text = transcribe_voice(msg["voice"]["file_id"])
                    except Exception as exc:
                        logger.exception("Voice transcription failed for reflection reply")
                        send_message(chat_id, f"Could not transcribe voice: {exc}")
                        continue
                if user_text:
                    try:
                        process_reflection_response(
                            pending, user_text, chat_id,
                            str(settings.obsidian_vault_path),
                            invoke_claude, send_message, state,
                        )
                        # Record revisit if this was a revisit reflection
                        revisit_filename = pending.get("revisit_note_filename")
                        if revisit_filename:
                            record_revisit(state, revisit_filename)
                    except Exception:
                        logger.exception("Reflection response processing failed")
                        send_message(chat_id, "Failed to process reflection response.")
                continue

        if text.startswith("/save"):
            force_ready_chat_ids.add(chat_id)
            save_update_ids.add(upd["update_id"])
            send_message(chat_id, "Processing current session immediately...")
        elif text.startswith("/clear"):
            cleared_chat_ids.add(chat_id)
            send_message(chat_id, "Session cleared. Start a new message to begin a fresh session.")
        elif text.startswith("/find"):
            query = text[len("/find"):].strip()
            if not query:
                send_message(chat_id, "Usage: /find <query>\nExample: /find reproducibility")
            else:
                try:
                    results = search_vault(query, str(settings.obsidian_vault_path), top_k=5)
                    if not results:
                        send_message(chat_id, f'No results for "{query}". Is Smart Connections configured?')
                    else:
                        lines = [f'Found {len(results)} notes matching "{query}":\n']
                        for i, r in enumerate(results, 1):
                            lines.append(f"{i}. [[{r['title']}]] ({r['score']:.2f})")
                        send_message(chat_id, "\n".join(lines))
                except Exception as exc:
                    logger.exception("Search failed for query: %s", query)
                    send_message(chat_id, f"Search error: {exc}")
        elif text.startswith("/ask"):
            query = text[len("/ask"):].strip()
            if not query:
                send_message(chat_id, "Usage: /ask <question>\nExample: /ask What do I know about spaced repetition?")
            else:
                handle_ask_command(query, chat_id, upd["update_id"])
        elif text.startswith("/revisit"):
            state.update(
                handle_revisit_command(text, chat_id, state, send_message, invoke_claude)
            )
        elif text.startswith("/reflect"):
            state.update(
                handle_reflect_command(text, chat_id, state, send_message, invoke_claude)
            )
        elif text.startswith("/help"):
            send_message(
                chat_id,
                "Commands:\n"
                "/save — process current session immediately\n"
                "/clear — discard current session\n"
                "/find <query> — search vault notes by semantic similarity\n"
                "/ask <question> — ask a question across your vault notes\n"
                "/revisit [topic] — revisit a saved note with guided reflection\n"
                "/reflect — manage reflection chains (list, trigger, pause, ...)\n"
                "/help — show this message\n\n"
                "Send a URL, voice note, image, or PDF to save a resource to your Obsidian vault.\n"
                "Start a message with 'todo: ' to add a task directly to your todo inbox.\n"
                "Send a photo with caption starting 'todo' to extract tasks from the image.",
            )
        elif text.lower().startswith("todo:"):
            task_text = text[5:].strip()
            if task_text:
                process_todo_message(task_text, chat_id)
            else:
                send_message(chat_id, "Usage: todo: <task description>")
        elif msg.get("photo") and msg.get("caption", "").lower().startswith("todo"):
            best = max(msg["photo"], key=lambda p: p.get("file_size", 0))
            process_image_todo(best["file_id"], upd["update_id"], chat_id)
        else:
            remaining.append(upd)

    return remaining


# ---------------------------------------------------------------------------
# /ask — RAG question-answering
# ---------------------------------------------------------------------------


def _strip_note_metadata(content: str) -> str:
    """Strip frontmatter, attachment embeds, and footer from a vault note."""
    # Strip YAML frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    # Strip attachment embeds at the bottom
    lines = content.split("\n")
    while lines and (
        re.match(r"^!\[\[Attachments/", lines[-1])
        or lines[-1].strip() == ""
    ):
        lines.pop()
    # Strip "Saved via Telegram" footer
    while lines and (
        lines[-1].strip().startswith("*Saved via Telegram")
        or lines[-1].strip() == "---"
        or lines[-1].strip() == ""
    ):
        lines.pop()
    return "\n".join(lines)


def handle_ask_command(query: str, chat_id: int, update_id: int) -> None:
    """RAG search: find related notes, read them, ask Claude, send answer."""
    vault_path = str(settings.obsidian_vault_path)

    send_message(chat_id, "Working on it...")

    # Semantic search
    try:
        results = search_vault(query, vault_path, top_k=5)
    except Exception as exc:
        logger.exception("Search failed for /ask query: %s", query)
        send_message(chat_id, f"Search error: {exc}")
        return

    if not results:
        send_message(chat_id, f'No notes found matching "{query}". Is Smart Connections configured?')
        return

    # Read matched notes with token budget
    MAX_CONTEXT_CHARS = 80_000
    notes_parts: list[str] = []
    total_chars = 0
    notes_used = 0

    for r in results:
        note_path = Path(vault_path) / r["path"]
        if not note_path.exists():
            logger.warning("Note file not found: %s", note_path)
            continue
        try:
            content = note_path.read_text(errors="ignore")
        except OSError:
            logger.warning("Could not read note: %s", note_path)
            continue

        cleaned = _strip_note_metadata(content)
        if total_chars + len(cleaned) > MAX_CONTEXT_CHARS and notes_used > 0:
            break

        notes_parts.append(
            f"### [[{r['title']}]] (similarity: {r['score']:.2f})\n\n{cleaned}"
        )
        total_chars += len(cleaned)
        notes_used += 1

    if not notes_parts:
        send_message(chat_id, "Found matching notes but could not read any of them from disk.")
        return

    notes_block = "\n\n---\n\n".join(notes_parts)

    # Build prompt
    template = ASK_PROMPT_TEMPLATE.read_text()
    prompt = (
        template
        .replace("{question}", query)
        .replace("{notes_block}", notes_block)
        .replace("{note_count}", str(notes_used))
    )

    # Invoke Claude
    try:
        answer = invoke_claude(prompt, update_id, vault_path)
    except Exception as exc:
        logger.exception("Claude failed for /ask query: %s", query)
        send_message(chat_id, f"Failed to generate answer: {exc}")
        return

    answer = answer.strip()
    if not answer:
        send_message(chat_id, "Claude returned an empty response. Try rephrasing your question.")
        return

    # Build reply with sources header
    sources = ", ".join(f"[[{r['title']}]]" for r in results[:notes_used])
    header = f"Sources: {sources}\n\n"
    full_reply = header + answer
    if len(full_reply) > 4096:
        full_reply = full_reply[:4093] + "..."

    send_message(chat_id, full_reply)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_prompt(session: list[dict], resource_folder: str, vault_path: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    template = PROMPT_TEMPLATE.read_text()

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    datetime_str = now.strftime("%Y-%m-%d %H:%M")

    urls: list[str] = []
    voice_texts: list[str] = []
    voice_todo_texts: list[str] = []
    plain_texts: list[str] = []
    image_paths: list[str] = []
    pdf_texts: list[str] = []
    fetch_failed = False
    failed_urls: list[str] = []
    content_parts: list[str] = []
    unsupported_docs: list[str] = []
    pending_attachments: list[tuple[str, bytes]] = []  # (extension, raw_bytes) — saved after title is known
    source_url_or_type = "attachment"
    source_id = ""

    for upd in session:
        msg = upd.get("message") or upd.get("edited_message") or {}
        update_id = upd["update_id"]

        # Voice note
        if "voice" in msg:
            logger.info("Transcribing voice note...")
            text = transcribe_voice(msg["voice"]["file_id"])
            m = _TODO_RE.match(text)
            if m:
                voice_todo_texts.append(text[m.end():].strip())
                logger.info("Voice todo detected: %s", text[:80])
            else:
                voice_texts.append(text)

        # Photo / image
        elif "photo" in msg:
            # Telegram sends multiple resolutions; use the largest
            best = max(msg["photo"], key=lambda p: p.get("file_size", 0))
            path, img_bytes = save_image(best["file_id"], update_id)
            image_paths.append(path)
            pending_attachments.append((".jpg", img_bytes))
            caption = msg.get("caption", "")
            if caption:
                plain_texts.append(caption)

        # Document (PDF, text-based, or other)
        elif "document" in msg:
            doc = msg["document"]
            mime = doc.get("mime_type", "")
            original_name = doc.get("file_name", f"attachment_{update_id}")
            if mime == "application/pdf":
                logger.info("Extracting PDF text...")
                text, pdf_bytes = extract_pdf(doc["file_id"])
                pdf_texts.append(text)
                pending_attachments.append((".pdf", pdf_bytes))
                source_url_or_type = "pdf"
            else:
                # Try to read as a text document
                logger.info("Attempting text extraction for %s (mime: %s)", original_name, mime)
                text = extract_text_document(doc["file_id"], original_name)
                if text is not None:
                    # Determine file extension; default to .md
                    name_path = Path(original_name)
                    ext = name_path.suffix if name_path.suffix not in ("", ".") else ".md"
                    logger.info("Extracted text from %s (%d chars)", original_name, len(text))
                    pending_attachments.append((ext, text.encode("utf-8")))
                    content_parts.append(f"--- Content of {original_name} ---\n{text}")
                    source_url_or_type = f"document:{original_name}"
                else:
                    logger.warning("Unsupported document: %s (mime: %s)", original_name, mime)
                    unsupported_docs.append(original_name)

        # Text message — may contain a URL or be plain context
        elif "text" in msg or "caption" in msg:
            text = msg.get("text") or msg.get("caption", "")
            # Detect bare URLs
            url_match = re.search(r"https?://\S+", text)
            if url_match:
                url = url_match.group(0).rstrip(".,;)")
                urls.append(url)
                # Non-URL portion may be additional context
                context_text = text.replace(url, "").strip()
                if context_text:
                    plain_texts.append(context_text)
            else:
                plain_texts.append(text)

    # Fetch URLs
    for url in urls:
        if is_youtube_url(url):
            fetched, failed = fetch_youtube_transcript(url)
            if failed:
                # Fall back to Jina Reader for page title/description
                fetched, failed = fetch_url(url)
        else:
            fetched, failed = fetch_url(url)
        if failed:
            fetch_failed = True
            failed_urls.append(url)
            source_url_or_type = url
        else:
            content_parts.append(fetched)
            source_url_or_type = url
            # Save fetched page as a markdown attachment
            pending_attachments.append((".md", fetched.encode("utf-8")))

    # Add PDF texts
    content_parts.extend(pdf_texts)

    # Compute source_id
    if urls:
        source_id = sha256_of(urls[0])
    elif pdf_texts:
        source_id = sha256_of("".join(pdf_texts)[:500])
    elif image_paths:
        source_id = sha256_of(image_paths[0])
    else:
        source_id = sha256_of("".join(plain_texts)[:500])

    # Build content block
    failed_block = ""
    if failed_urls:
        failed_lines = "\n".join(f"  - {u}" for u in failed_urls)
        failed_block = (
            f"[URL FETCH FAILED]\n"
            f"The following URLs could NOT be retrieved:\n{failed_lines}"
        )

    if content_parts and failed_block:
        content_block = failed_block + "\n\n---\n\n" + "\n\n---\n\n".join(content_parts)
    elif failed_block:
        content_block = failed_block
    elif content_parts:
        content_block = "\n\n---\n\n".join(content_parts)
    else:
        content_block = "(No URL or document content — context-only note)"

    # Voice / context
    voice_context = "\n".join(voice_texts + plain_texts) if (voice_texts or plain_texts) else "No context provided"

    # Image line
    image_lines = []
    for p in image_paths:
        image_lines.append(
            f"There is an image at {p}. Use your Read tool to view it. "
            f"Default to a concise description or summary of its content — do NOT transcribe it in full "
            f"unless the user context explicitly requests transcription or text extraction."
        )
    image_line_block = "\n".join(image_lines) if image_lines else ""

    attachment_line_block = ""

    # Semantic search for related notes
    related_notes_block = ""
    try:
        search_query = " ".join(content_parts[:3])[:1000] if content_parts else voice_context
        if search_query and search_query != "No context provided":
            results = search_vault(search_query, vault_path, top_k=10)
            if results:
                lines = ["## Existing vault notes most related to this content (by semantic similarity):"]
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. {r['path']} ({r['score']:.2f})")
                related_notes_block = "\n".join(lines)
    except Exception:
        logger.exception("Semantic search failed during prompt building — continuing without")

    # Collect existing vault tags for reuse
    existing_tags_block = ""
    try:
        tags = [t for t in collect_vault_tags(vault_path) if t not in ("resource", "reading-list")]
        if tags:
            existing_tags_block = "Existing vault tags: " + ", ".join(tags)
    except Exception:
        logger.exception("Tag collection failed — continuing without")

    filled = (
        template
        .replace("{content_or_fetch_failed_message}", content_block)
        .replace("{voice_context_or_none}", voice_context)
        .replace("{source_url_or_type}", source_url_or_type)
        .replace("{source_id}", source_id)
        .replace("{date}", date_str)
        .replace("{datetime}", datetime_str)
        .replace("{image_line_if_present}", (image_line_block + "\n" + attachment_line_block).strip())
        .replace("{resource_folder}", resource_folder)
        .replace("{related_notes}", related_notes_block)
        .replace("{existing_tags}", existing_tags_block)
    )

    has_saveable_content = bool(urls or pdf_texts or image_paths or plain_texts or voice_texts)
    return filled, unsupported_docs, pending_attachments, voice_todo_texts, has_saveable_content


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def invoke_claude(prompt: str, session_id: int, vault_path: str, model: str = "claude-sonnet-4-6") -> str:
    """Write prompt to /tmp file, pipe to claude CLI, return stdout."""
    tmp_path = Path(f"/tmp/bridge_prompt_{session_id}.md")
    try:
        tmp_path.write_text(prompt)
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # allow nested invocation from within a Claude Code session
        with open(tmp_path) as f:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", "--model", model],
                stdin=f,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=vault_path,
                env=env,
            )
        if result.returncode != 0:
            logger.error("Claude stderr: %s", result.stderr[:1000])
            raise RuntimeError(f"Claude exited with code {result.returncode}")
        return result.stdout
    finally:
        tmp_path.unlink(missing_ok=True)


def parse_saved_filename(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("SAVED:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# Session processing
# ---------------------------------------------------------------------------


def process_session(session: list[dict]) -> None:
    chat_id = (
        (session[0].get("message") or session[0].get("edited_message") or {})
        .get("chat", {})
        .get("id")
    )
    session_id = session_max_update_id(session)
    vault_path = str(settings.obsidian_vault_path)
    resource_folder = settings.obsidian_resource_folder

    logger.info("Processing session %s (%d messages)", session_id, len(session))

    try:
        prompt, unsupported_docs, pending_attachments, todo_texts, has_saveable_content = build_prompt(
            session, resource_folder, vault_path
        )

        # Handle voice todos immediately, independently of any other content
        for task_text in todo_texts:
            process_todo_message(task_text, chat_id)

        if unsupported_docs:
            names = ", ".join(unsupported_docs)
            send_message(chat_id, f"Skipped unsupported file(s): {names}")

        # Nothing left to summarise — all content was voice todos
        if not has_saveable_content:
            return

        stdout = invoke_claude(prompt, session_id, vault_path)
        filename = parse_saved_filename(stdout)

        if filename is None:
            logger.warning(
                "Claude did not return SAVED: line for session %s. Output: %s",
                session_id,
                stdout[:500],
            )
            send_message(
                chat_id,
                f"Note processing failed — Claude did not save a file. Response:\n{stdout[:300]}",
            )
            return

        note_title = Path(filename).stem

        # Save pending attachments with the note title
        attachments_dir = Path(vault_path) / "Attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        saved_attachment_names: list[str] = []
        for i, (ext, raw_bytes) in enumerate(pending_attachments):
            suffix = f" ({i + 1})" if len(pending_attachments) > 1 else ""
            doc_name = f"{note_title}{suffix}{ext}"
            save_to = attachments_dir / doc_name
            save_to.write_bytes(raw_bytes)
            saved_attachment_names.append(doc_name)
            logger.info("Attachment saved to %s", save_to)

        # Append attachment links to the end of the note
        if saved_attachment_names:
            # Use basename only — Claude sometimes returns the full relative path in SAVED:
            note_path = Path(vault_path) / resource_folder / Path(filename).name
            if not note_path.exists():
                logger.warning(
                    "Note file not found at %s — attachment links not appended", note_path
                )
            if note_path.exists():
                note_content = note_path.read_text()
                embed_lines = "\n".join(f"![[Attachments/{name}]]" for name in saved_attachment_names)
                note_content = note_content.rstrip("\n") + "\n\n" + embed_lines + "\n"
                note_path.write_text(note_content)

        # Count related notes mentioned
        related_count = stdout.count("[[") - stdout.count("[[Note Title]]")
        related_count = max(0, related_count)

        # Publish to GitHub Pages (if configured)
        note_path_for_reply = Path(vault_path) / resource_folder / Path(filename).name
        pages_url = None
        if settings.pages_repo_path and settings.pages_base_url:
            try:
                pages_url = publish_note(
                    note_path_for_reply,
                    settings.pages_repo_path,
                    settings.pages_base_url,
                    Path(vault_path),
                )
                if pages_url:
                    git_commit_and_push(
                        settings.pages_repo_path,
                        f"Publish: {note_title}",
                    )
            except Exception:
                logger.exception("GitHub Pages publish failed for %s — continuing", note_title)

        # Build reply
        reply = f"Saved: [[{note_title}]]"
        if related_count:
            reply += f" — {related_count} related note{'s' if related_count > 1 else ''} linked"

        if pages_url:
            reply += f"\n\n{pages_url}"
            # Add a short preview (first few lines of body, stripped of markup)
            if note_path_for_reply.exists():
                note_body = note_path_for_reply.read_text()
                if note_body.startswith("---"):
                    end = note_body.find("---", 3)
                    if end != -1:
                        note_body = note_body[end + 3:].strip()
                # Grab the first few non-empty, non-heading lines as preview
                preview_lines = []
                for line in note_body.split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith(">") or line.startswith("---"):
                        continue
                    preview_lines.append(line)
                    if len(preview_lines) >= 3:
                        break
                if preview_lines:
                    preview = "\n".join(preview_lines)
                    max_preview = 4096 - len(reply) - 10
                    if len(preview) > max_preview:
                        preview = preview[:max_preview] + "…"
                    reply += f"\n\n{preview}"
        else:
            # Fall back to inline note body (original behaviour)
            note_body = ""
            if note_path_for_reply.exists():
                note_body = note_path_for_reply.read_text()
                if note_body.startswith("---"):
                    end = note_body.find("---", 3)
                    if end != -1:
                        note_body = note_body[end + 3:].strip()
            if note_body:
                max_body = 4096 - len(reply) - 10
                if len(note_body) > max_body:
                    note_body = note_body[:max_body] + "…"
                reply += f"\n\n{note_body}"

        send_message(chat_id, reply)
        logger.info("Session %s → %s", session_id, filename)

    except Exception as exc:
        logger.exception("Failed to process session %s", session_id)
        if chat_id:
            exc_str = str(exc)
            # Transcription errors happen before any note is saved — use a more accurate prefix
            if exc_str.startswith(("Could not download voice note", "Whisper transcription failed")):
                send_message(chat_id, f"Voice note error: {exc_str}")
            else:
                send_message(chat_id, f"Error saving note: {exc_str}")
        # Only backoff on quota/rate-limit or Claude failures — not transient network errors
        exc_str = str(exc).lower()
        is_quota = any(kw in exc_str for kw in ("quota", "rate", "limit", "429", "overloaded", "capacity"))
        is_transient_network = any(h in exc_str for h in ("connection reset", "econnreset", "errno 54", "timed out", "timeout", "connection refused"))
        if is_quota or (not is_transient_network):
            backoff_seconds = 900
            state = load_state()
            state["backoff_until"] = time.time() + backoff_seconds
            save_state(state)
            logger.info("Backoff set for %d seconds (quota-related: %s)", backoff_seconds, is_quota)
        else:
            logger.info("Transient network error — no backoff, will retry on next poll")
        raise

    finally:
        # Clean up any temp image files from this session
        for upd in session:
            update_id = upd["update_id"]
            img_path = Path(f"/tmp/bridge_img_{update_id}.jpg")
            img_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------


def run_poll() -> None:
    state = load_state()
    last_offset = state.get("last_offset", 0)

    # Check backoff — skip this poll if we're still in a cooldown period
    backoff_until = state.get("backoff_until", 0)
    if time.time() < backoff_until:
        remaining = int(backoff_until - time.time())
        logger.info("In backoff period (%d seconds remaining). Skipping poll.", remaining)
        return

    # Check and send due reflection prompts
    try:
        chat_id_for_reflections = settings.telegram_allowed_user_id
        state = check_and_send_reflections(state, chat_id_for_reflections, send_message, invoke_claude)
        save_state(state)
    except Exception:
        logger.exception("Reflection check failed — continuing with normal poll")

    logger.info("Polling with offset=%s", last_offset + 1)
    updates = get_updates(last_offset + 1)

    if not updates:
        logger.info("No new updates.")
        return

    force_ready_chat_ids: set[int] = set()
    cleared_chat_ids: set[int] = set()
    save_update_ids: set[int] = set()

    # Handle commands first; get back non-command updates
    remaining = handle_commands(updates, force_ready_chat_ids, cleared_chat_ids, save_update_ids, state=state)

    # Advance offset past all command updates (they're fully handled)
    command_update_ids = {
        upd["update_id"] for upd in updates if upd not in remaining
    }
    if command_update_ids:
        last_offset = max(last_offset, max(command_update_ids))

    sessions = group_into_sessions(remaining, split_after_update_ids=save_update_ids)
    logger.info("Grouped %d updates into %d session(s)", len(remaining), len(sessions))

    for session in sessions:
        chat_id = (
            (session[0].get("message") or session[0].get("edited_message") or {})
            .get("chat", {})
            .get("id")
        )

        # Drop cleared sessions
        if chat_id in cleared_chat_ids:
            logger.info("Discarding cleared session for chat %s", chat_id)
            last_offset = max(last_offset, session_max_update_id(session))
            continue

        # Check if session is ready (timeout elapsed or /save issued)
        age = session_last_message_age(session)
        forced = chat_id in force_ready_chat_ids
        if age < settings.session_timeout_seconds and not forced:
            logger.info(
                "Session not ready yet (last message %ds ago, timeout=%ds). Skipping.",
                int(age),
                settings.session_timeout_seconds,
            )
            continue  # do NOT advance offset past these updates

        process_session(session)
        last_offset = max(last_offset, session_max_update_id(session))

    state["last_offset"] = last_offset
    state["backoff_until"] = 0
    save_state(state)
    logger.info("State saved with last_offset=%s", last_offset)


POLL_INTERVAL_SECONDS = 120

_shutdown = False


def _handle_signal(signum, _frame) -> None:
    global _shutdown
    _shutdown = True
    logger.info("Received signal %d — finishing current cycle then exiting.", signum)


def main() -> None:
    # Long-lived loop by default; `--once` runs a single poll (manual/testing).
    # The daemon stays resident so steady-state polling never depends on launchd
    # repeatedly spawning new processes — which can silently stop after long
    # uptime / heavy sleep cycling, even though the agent is still "loaded".
    once = "--once" in sys.argv
    if not acquire_lock():
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        if once:
            run_poll()
            return
        logger.info("Starting long-lived poll loop (interval=%ds).", POLL_INTERVAL_SECONDS)
        while not _shutdown:
            try:
                run_poll()
            except Exception:
                logger.exception("Poll cycle failed — continuing loop.")
            # Sleep in 1s slices so a shutdown signal is acted on promptly.
            for _ in range(POLL_INTERVAL_SECONDS):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        release_lock()
        logger.info("Poll loop stopped; lock released.")


if __name__ == "__main__":
    main()
