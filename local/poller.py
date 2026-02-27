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
from media import extract_pdf, extract_text_document, fetch_url, save_image, sha256_of, transcribe_voice

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


def group_into_sessions(updates: list[dict]) -> list[list[dict]]:
    """
    Group updates from the allowed user into sessions.
    A new session starts when there is a >SESSION_GAP second gap between messages.
    Updates from other senders are silently dropped.
    """
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
        if gap > SESSION_GAP:
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
# Command handling
# ---------------------------------------------------------------------------


def handle_commands(
    updates: list[dict],
    force_ready_chat_ids: set[int],
    cleared_chat_ids: set[int],
) -> list[dict]:
    """
    Process bot commands. Returns remaining (non-command) updates.
    Modifies force_ready_chat_ids and cleared_chat_ids in place.
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
            send_message(chat_id, "Processing current session immediately...")
        elif text.startswith("/clear"):
            cleared_chat_ids.add(chat_id)
            send_message(chat_id, "Session cleared. Start a new message to begin a fresh session.")
        elif text.startswith("/help"):
            send_message(
                chat_id,
                "Commands:\n"
                "/save — process current session immediately\n"
                "/clear — discard current session\n"
                "/help — show this message\n\n"
                "Send a URL, voice note, image, or PDF to save a resource to your Obsidian vault.",
            )
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
            path = save_image(best["file_id"], update_id)
            image_paths.append(path)

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
        fetched, failed = fetch_url(url)
        if failed:
            fetch_failed = True
            source_url_or_type = url
        else:
            content_parts.append(fetched)
            source_url_or_type = url

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
            f"There is an image at {p}. Use your Read tool to view it and describe its content in the summary."
        )
    image_line_block = "\n".join(image_lines) if image_lines else ""

    # Attachment embed instructions — filenames are determined after Claude picks a title
    num_pdfs = sum(1 for ext, _ in pending_attachments if ext == ".pdf")
    if num_pdfs:
        pdf_line_block = (
            f"There {'is' if num_pdfs == 1 else 'are'} {num_pdfs} PDF attachment(s) that will be saved to the vault. "
            "Include the line `![[ATTACHMENT_PLACEHOLDER]]` in the note body after the summary — "
            "the actual filename will be filled in automatically."
        )
    else:
        pdf_line_block = ""

    filled = (
        template
        .replace("{content_or_fetch_failed_message}", content_block)
        .replace("{voice_context_or_none}", voice_context)
        .replace("{source_url_or_type}", source_url_or_type)
        .replace("{source_id}", source_id)
        .replace("{date}", date_str)
        .replace("{datetime}", datetime_str)
        .replace("{image_line_if_present}", (image_line_block + "\n" + pdf_line_block).strip())
        .replace("{resource_folder}", resource_folder)
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
                ["claude", "--print", "--dangerously-skip-permissions"],
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

        # Replace placeholder in the saved note with actual attachment filenames
        if saved_attachment_names:
            note_path = Path(vault_path) / resource_folder / filename
            if note_path.exists():
                note_content = note_path.read_text()
                embed_lines = "\n".join(f"![[Attachments/{name}]]" for name in saved_attachment_names)
                note_content = note_content.replace("![[ATTACHMENT_PLACEHOLDER]]", embed_lines)
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

    # Handle commands first; get back non-command updates
    remaining = handle_commands(updates, force_ready_chat_ids, cleared_chat_ids)

    # Advance offset past all command updates (they're fully handled)
    command_update_ids = {
        upd["update_id"] for upd in updates if upd not in remaining
    }
    if command_update_ids:
        last_offset = max(last_offset, max(command_update_ids))

    sessions = group_into_sessions(remaining)
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
