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
import re
import tempfile
from pathlib import Path

import httpx
import pdfplumber
from openai import OpenAI
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter

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


_YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch|shorts|live|embed)|youtu\.be/)",
)

_VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})",
)

YOUTUBE_MIN_LENGTH = 50  # shorter than Jina; short videos can have brief valid transcripts


def is_youtube_url(url: str) -> bool:
    """Return True if *url* points to a YouTube video page."""
    return bool(_YOUTUBE_URL_RE.search(url))


def extract_video_id(url: str) -> str | None:
    """Extract the 11-char video ID from any YouTube URL format."""
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def fetch_youtube_transcript(url: str) -> tuple[str | None, bool]:
    """
    Fetch the transcript for a YouTube video.

    Returns the same (content, fetch_failed) signature as fetch_url().
    """
    video_id = extract_video_id(url)
    if not video_id:
        logger.warning("Could not extract video ID from %s", url)
        return None, True

    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id)
        text = TextFormatter().format_transcript(transcript)
        if len(text) < YOUTUBE_MIN_LENGTH:
            logger.warning("YouTube transcript too short (%d chars) for %s", len(text), url)
            return None, True

        language = transcript.language
        header = (
            f"[YouTube Video Transcript]\n"
            f"URL: {url}\n"
            f"Language: {language}\n\n"
        )
        return header + text, False
    except Exception as exc:
        logger.warning("YouTube transcript fetch failed for %s: %s", url, exc)
        return None, True


MAX_PDF_PAGES = 35  # hard limit to avoid oversized prompts


def extract_pdf(file_id: str) -> tuple[str, bytes]:
    """
    Download a Telegram PDF and extract its text with pdfplumber.
    Returns (extracted_text, raw_pdf_bytes).
    Text extraction is limited to the first MAX_PDF_PAGES pages.
    """
    pdf_bytes = _download_bytes(file_id)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        pages = []
        with pdfplumber.open(tmp_path) as pdf:
            total_pages = len(pdf.pages)
            pages_to_read = pdf.pages[:MAX_PDF_PAGES]
            if total_pages > MAX_PDF_PAGES:
                logger.warning(
                    "PDF has %d pages — extracting only first %d",
                    total_pages,
                    MAX_PDF_PAGES,
                )
            for page in pages_to_read:
                text = page.extract_text()
                if text:
                    pages.append(text)
        text = "\n\n".join(pages)
        if total_pages > MAX_PDF_PAGES:
            text += f"\n\n[Note: PDF has {total_pages} pages; only the first {MAX_PDF_PAGES} were extracted.]"
        return text, pdf_bytes
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


def save_image(file_id: str, update_id: int) -> tuple[str, bytes]:
    """Download a Telegram image and save it to /tmp. Returns (local_path, image_bytes)."""
    img_bytes = _download_bytes(file_id)
    dest = Path(f"/tmp/bridge_img_{update_id}.jpg")
    dest.write_bytes(img_bytes)
    logger.info("Image saved to %s", dest)
    return str(dest), img_bytes


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
