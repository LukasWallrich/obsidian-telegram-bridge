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

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
SESSION_GAP = 300  # seconds — gap between messages that starts a new session

# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        logger.info("Lock file exists — another instance is running. Exiting.")
        return False
    LOCK_FILE.touch()
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


def send_message(chat_id: int, text: str) -> None:
    tg_post("sendMessage", chat_id=chat_id, text=text)


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


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------


def handle_commands(
    updates: list[dict],
    force_ready_chat_ids: set[int],
    cleared_chat_ids: set[int],
    save_update_ids: set[int],
) -> list[dict]:
    """
    Process bot commands. Returns remaining (non-command) updates.
    Modifies force_ready_chat_ids, cleared_chat_ids, and save_update_ids in place.
    save_update_ids collects update_ids of /save commands so they can be used
    as session split points.
    """
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
        elif text.startswith("/help"):
            send_message(
                chat_id,
                "Commands:\n"
                "/save — process current session immediately\n"
                "/clear — discard current session\n"
                "/find <query> — search vault notes by semantic similarity\n"
                "/help — show this message\n\n"
                "Send a URL, voice note, image, or PDF to save a resource to your Obsidian vault.\n"
                "Start a message with 'todo: ' to add a task directly to your todo inbox.",
            )
        elif text.lower().startswith("todo:"):
            task_text = text[5:].strip()
            if task_text:
                process_todo_message(task_text, chat_id)
            else:
                send_message(chat_id, "Usage: todo: <task description>")
        else:
            remaining.append(upd)

    return remaining


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
    plain_texts: list[str] = []
    image_paths: list[str] = []
    pdf_texts: list[str] = []
    fetch_failed = False
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
    if fetch_failed and not content_parts:
        content_block = (
            f"[URL FETCH FAILED]\n"
            f"The URL fetch failed for: {source_url_or_type}\n"
            f"Retrieve this URL yourself using your web tools or /browser-use skill."
        )
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

    return filled, unsupported_docs, pending_attachments


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def invoke_claude(prompt: str, session_id: int, vault_path: str) -> str:
    """Write prompt to /tmp file, pipe to claude CLI, return stdout."""
    tmp_path = Path(f"/tmp/bridge_prompt_{session_id}.md")
    try:
        tmp_path.write_text(prompt)
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # allow nested invocation from within a Claude Code session
        with open(tmp_path) as f:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", "--model", "claude-sonnet-4-6"],
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
        prompt, unsupported_docs, pending_attachments = build_prompt(session, resource_folder, vault_path)
        if unsupported_docs:
            names = ", ".join(unsupported_docs)
            send_message(chat_id, f"Skipped unsupported file(s): {names}")
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

        reply = f"Saved: [[{note_title}]]"
        if related_count:
            reply += f" — {related_count} related note{'s' if related_count > 1 else ''} linked"

        send_message(chat_id, reply)
        logger.info("Session %s → %s", session_id, filename)

    except Exception as exc:
        logger.exception("Failed to process session %s", session_id)
        if chat_id:
            send_message(chat_id, f"Error saving note: {exc}")
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

    logger.info("Polling with offset=%s", last_offset + 1)
    updates = get_updates(last_offset + 1)

    if not updates:
        logger.info("No new updates.")
        return

    force_ready_chat_ids: set[int] = set()
    cleared_chat_ids: set[int] = set()
    save_update_ids: set[int] = set()

    # Handle commands first; get back non-command updates
    remaining = handle_commands(updates, force_ready_chat_ids, cleared_chat_ids, save_update_ids)

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

    save_state({"last_offset": last_offset})
    logger.info("State saved with last_offset=%s", last_offset)


def main() -> None:
    if not acquire_lock():
        sys.exit(0)
    try:
        run_poll()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
