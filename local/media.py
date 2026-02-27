"""
media.py — Download and process Telegram media attachments.

Handlers:
  - Voice (.ogg): download → OpenAI Whisper → transcribed text
  - URL: Jina Reader fetch → Markdown text (or fetch_failed flag)
  - PDF: download → pdfplumber text extraction
  - Image: download → save to /tmp/bridge_img_{update_id}.jpg → local path
  - Text documents (.md, .txt, etc.): download → validate text → content string
  - Plain text: pass through
"""

import hashlib
import logging
import tempfile
from pathlib import Path

import httpx
import pdfplumber
from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

JINA_BASE = "https://r.jina.ai/"
JINA_MIN_LENGTH = 200  # chars below this = treat as failed


def _telegram_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"


def _get_telegram_file_path(file_id: str) -> str:
    """Resolve a Telegram file_id to a downloadable path."""
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile"
    r = httpx.get(url, params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["file_path"]


def _download_bytes(file_id: str) -> bytes:
    file_path = _get_telegram_file_path(file_id)
    url = _telegram_file_url(file_path)
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content


def transcribe_voice(file_id: str) -> str:
    """Download a Telegram voice note and transcribe it with Whisper."""
    audio_bytes = _download_bytes(file_id)
    client = OpenAI(api_key=settings.openai_api_key)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = Path(tmp.name)

    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        return transcript.text
    finally:
        tmp_path.unlink(missing_ok=True)


def fetch_url(url: str) -> tuple[str | None, bool]:
    """
    Fetch URL content via Jina Reader.

    Returns:
        (content, fetch_failed)
        content is None when fetch_failed is True.
    """
    jina_url = f"{JINA_BASE}{url}"
    headers = {}
    if settings.jina_api_key:
        headers["Authorization"] = f"Bearer {settings.jina_api_key}"

    try:
        r = httpx.get(jina_url, headers=headers, timeout=30, follow_redirects=True)
        r.raise_for_status()
        text = r.text.strip()
        if len(text) < JINA_MIN_LENGTH:
            logger.warning("Jina returned <200 chars for %s — marking fetch_failed", url)
            return None, True
        return text, False
    except Exception as exc:
        logger.warning("Jina fetch failed for %s: %s", url, exc)
        return None, True


def extract_pdf(file_id: str) -> tuple[str, bytes]:
    """
    Download a Telegram PDF and extract its text with pdfplumber.
    Returns (extracted_text, raw_pdf_bytes).
    """
    pdf_bytes = _download_bytes(file_id)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        pages = []
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
        return "\n\n".join(pages), pdf_bytes
    finally:
        tmp_path.unlink(missing_ok=True)


def extract_text_document(file_id: str, filename: str) -> str | None:
    """
    Download a Telegram document and try to read it as text.
    Returns the text content, or None if the file doesn't appear to be text.
    """
    raw = _download_bytes(file_id)

    # Quick binary check: if >10% of the first 1024 bytes are non-text
    # control characters, it's probably not a text file.
    sample = raw[:1024]
    if not sample:
        return None
    non_text = sum(
        1 for b in sample
        if b < 0x09 or (0x0E <= b < 0x20 and b != 0x1B)
    )
    if non_text / len(sample) > 0.10:
        logger.info("File %s looks binary (%d%% control chars), skipping", filename, int(non_text / len(sample) * 100))
        return None

    # Try to decode as UTF-8, fall back to latin-1
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            logger.info("File %s could not be decoded as text", filename)
            return None

    return text


def save_image(file_id: str, update_id: int) -> str:
    """Download a Telegram image and save it to /tmp. Returns the local path."""
    img_bytes = _download_bytes(file_id)
    dest = Path(f"/tmp/bridge_img_{update_id}.jpg")
    dest.write_bytes(img_bytes)
    logger.info("Image saved to %s", dest)
    return str(dest)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
