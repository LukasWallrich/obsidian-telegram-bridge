"""
pages.py — Render Obsidian notes to HTML and publish to GitHub Pages.

Generates standalone HTML files with UUID-based slugs, resolves wikilinks
to clickable hrefs, embeds images, and pushes to a separate git repo
configured for GitHub Pages.
"""

import json
import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import jinja2
import markdown
from markupsafe import Markup

logger = logging.getLogger("bridge.pages")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}

# ---------------------------------------------------------------------------
# Slug map — bidirectional title ↔ slug mapping persisted as JSON
# ---------------------------------------------------------------------------


_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+")


class SlugMap:
    def __init__(self, pages_repo: Path):
        self.map_file = pages_repo / "_mapping.json"
        self.data: dict[str, str] = {}  # full title (with date) → slug
        self._short_index: dict[str, str] = {}  # title without date → slug
        self._load()

    def _load(self) -> None:
        if self.map_file.exists():
            self.data = json.loads(self.map_file.read_text())
            self._rebuild_short_index()

    def _rebuild_short_index(self) -> None:
        self._short_index = {}
        for title, slug in self.data.items():
            short = _DATE_PREFIX_RE.sub("", title)
            self._short_index[short] = slug

    def _save(self) -> None:
        self.map_file.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))

    def get_or_create(self, title: str) -> str:
        if title not in self.data:
            self.data[title] = uuid.uuid4().hex[:12]
            self._rebuild_short_index()
            self._save()
        return self.data[title]

    def resolve(self, title: str) -> str | None:
        """Resolve by full title or short title (without date prefix)."""
        return self.data.get(title) or self._short_index.get(title)


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and body. Simple key:value parser (no PyYAML)."""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            fm_text = text[3:end].strip()
            body = text[end + 3 :].strip()
            for line in fm_text.split("\n"):
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if val.startswith("[") and val.endswith("]"):
                        val = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
                    meta[key] = val
    return meta, body


# ---------------------------------------------------------------------------
# Wikilink resolution
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def resolve_wikilinks(md: str, slug_map: SlugMap, base_url: str) -> str:
    """Replace [[Title]] and [[Title|Display]] with <a> or <span>."""

    def _replace(m: re.Match) -> str:
        title = m.group(1).strip()
        display = (m.group(2) or title).strip()
        slug = slug_map.resolve(title)
        if slug:
            href = f"{base_url.rstrip('/')}/{slug}.html"
            return f'<a href="{href}">{display}</a>'
        return f'<span class="wikilink-unresolved">{display}</span>'

    return _WIKILINK_RE.sub(_replace, md)


# ---------------------------------------------------------------------------
# Obsidian callout conversion
# ---------------------------------------------------------------------------

_CALLOUT_RE = re.compile(
    r"^> \[!(\w+)\]\s*(.*)\n((?:> .*\n?)*)", re.MULTILINE
)


def convert_callouts(text: str) -> str:
    """Convert Obsidian > [!type] callouts to HTML divs."""

    def _replacer(m: re.Match) -> str:
        ctype = m.group(1).lower()
        title = m.group(2).strip() or ctype.title()
        body_lines = m.group(3).strip().split("\n")
        body = "\n".join(line.lstrip("> ").rstrip() for line in body_lines if line.strip())
        return (
            f'<div class="callout callout-{ctype}">'
            f'<div class="callout-title">{title}</div>'
            f'<div class="callout-content">\n\n{body}\n\n</div>'
            f"</div>\n"
        )

    return _CALLOUT_RE.sub(_replacer, text)


# ---------------------------------------------------------------------------
# Attachment embedding
# ---------------------------------------------------------------------------

_EMBED_RE = re.compile(r"!\[\[Attachments/([^\]]+)\]\]")


def embed_attachments(
    md: str, vault_path: Path, pages_repo: Path, slug: str
) -> str:
    """Replace ![[Attachments/file]] with <img> tags, copying images to pages repo."""

    def _replace(m: re.Match) -> str:
        filename = m.group(1)
        src_file = vault_path / "Attachments" / filename
        ext = Path(filename).suffix.lower()

        if ext in _IMAGE_EXTS:
            if src_file.exists():
                dest_dir = pages_repo / "img" / slug
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_dir / filename)
                return f'<img src="img/{slug}/{filename}" alt="{Path(filename).stem}">'
            return f'<span class="img-placeholder">Image not found: {filename}</span>'

        # Non-image attachments (PDFs, docs) — just mention them
        return f'<p><em>Attachment: {filename}</em></p>'

    return _EMBED_RE.sub(_replace, md)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _load_template() -> jinja2.Template:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=True,
    )
    return env.get_template("note.html")


def render_note(
    note_path: Path,
    slug_map: SlugMap,
    base_url: str,
    vault_path: Path,
    pages_repo: Path,
    slug: str,
    template: jinja2.Template,
) -> str:
    """Render a vault note to a standalone HTML page."""
    raw = note_path.read_text()
    meta, body = parse_frontmatter(raw)

    # Strip leading H1 (duplicate of template title)
    body = re.sub(r"^#\s+.+\n*", "", body, count=1)

    # Pre-process before markdown conversion
    body = resolve_wikilinks(body, slug_map, base_url)
    body = embed_attachments(body, vault_path, pages_repo, slug)
    body = convert_callouts(body)

    # Convert markdown to HTML
    html_body = markdown.markdown(body, extensions=["extra"])

    # Extract template variables from frontmatter
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]

    # Use clean title (strip date prefix if present)
    display_title = _DATE_PREFIX_RE.sub("", note_path.stem)

    return template.render(
        title=display_title,
        body=Markup(html_body),
        source_url=meta.get("source", ""),
        date=meta.get("date", ""),
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish_note(
    note_path: Path, pages_repo: Path, base_url: str, vault_path: Path
) -> str | None:
    """Render a note to HTML and write it to the pages repo. Returns the URL."""
    try:
        pages_repo = Path(pages_repo)
        slug_map = SlugMap(pages_repo)
        title = note_path.stem
        slug = slug_map.get_or_create(title)
        template = _load_template()

        html = render_note(
            note_path, slug_map, base_url, vault_path, pages_repo, slug, template
        )
        out_path = pages_repo / f"{slug}.html"
        out_path.write_text(html)
        logger.info("Rendered %s → %s", title, out_path.name)
        return f"{base_url.rstrip('/')}/{slug}.html"
    except Exception:
        logger.exception("Failed to render %s", note_path)
        return None


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


def git_commit_and_push(pages_repo: Path, message: str) -> bool:
    """Stage, commit, and push changes in the pages repo. Non-fatal on failure."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=pages_repo, check=True, capture_output=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=pages_repo, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("No changes to commit in pages repo")
            return True

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=pages_repo, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=pages_repo, check=True, capture_output=True, timeout=60,
        )
        logger.info("Pages repo pushed: %s", message)
        return True
    except subprocess.SubprocessError as exc:
        logger.error("Git operation failed in pages repo: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def backfill_all(
    vault_path: Path, resource_folder: str, pages_repo: Path, base_url: str
) -> int:
    """Render all existing vault notes to the pages repo. Returns count."""
    pages_repo = Path(pages_repo)
    vault_path = Path(vault_path)
    notes_dir = vault_path / resource_folder
    if not notes_dir.is_dir():
        logger.warning("Notes directory not found: %s", notes_dir)
        return 0

    slug_map = SlugMap(pages_repo)
    template = _load_template()
    count = 0

    for note_path in sorted(notes_dir.glob("*.md")):
        title = note_path.stem
        slug = slug_map.get_or_create(title)
        try:
            html = render_note(
                note_path, slug_map, base_url, vault_path, pages_repo, slug, template
            )
            out_path = pages_repo / f"{slug}.html"
            out_path.write_text(html)
            count += 1
            logger.info("Backfill: %s → %s", title, out_path.name)
        except Exception:
            logger.exception("Backfill failed for %s", note_path)

    return count
