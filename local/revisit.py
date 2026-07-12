"""
revisit.py — Spaced revisiting of saved notes with guided reflection.

Selects notes from the reading list using spaced-repetition priority,
gathers related context from the vault, and prompts the user to reflect
on how their thinking has evolved.
"""

import logging
import random
import re
from datetime import datetime
from pathlib import Path

from config import settings
from reflections import save_pending
from search import search_vault

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
PROMPTS_DIR = BASE_DIR / "prompts" / "reflections"

# Spaced-repetition intervals: revisit_count → days until next revisit
REVISIT_INTERVALS = {
    0: 7,
    1: 21,
    2: 60,
    3: 180,
}
DEFAULT_INTERVAL = 365  # 4+ revisits


# ---------------------------------------------------------------------------
# Note selection (spaced-repetition)
# ---------------------------------------------------------------------------


def _get_revisit_interval(revisit_count: int) -> int:
    """Return the target interval in days for a given revisit count."""
    return REVISIT_INTERVALS.get(revisit_count, DEFAULT_INTERVAL)


def select_revisit_note(
    vault_path: str,
    resource_folder: str,
    state: dict,
    topic: str | None = None,
) -> dict | None:
    """
    Select a note to revisit using spaced-repetition priority.

    Returns {"path": Path, "title": str, "priority": float} or None.
    """
    resource_dir = Path(vault_path) / resource_folder
    if not resource_dir.exists():
        return None

    today = datetime.now().date()
    min_age_days = 3
    revisit_history = state.get("revisit_history", {})

    # Step 1: Gather candidates
    candidates = []
    for f in resource_dir.glob("*.md"):
        match = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        days_since_saved = (today - file_date).days
        if days_since_saved < min_age_days:
            continue

        history = revisit_history.get(f.name, {})
        revisit_count = history.get("revisit_count", 0)
        revisit_dates = history.get("revisit_dates", [])

        if revisit_dates:
            try:
                last_revisit = datetime.strptime(revisit_dates[-1], "%Y-%m-%d").date()
                days_since_last = (today - last_revisit).days
            except ValueError:
                days_since_last = days_since_saved
        else:
            days_since_last = days_since_saved

        interval = _get_revisit_interval(revisit_count)
        priority = days_since_last / interval

        candidates.append({
            "path": f,
            "title": f.stem,
            "priority": priority,
            "revisit_count": revisit_count,
        })

    if not candidates:
        return None

    # Step 2: Topic filtering (optional)
    if topic:
        try:
            search_results = search_vault(topic, vault_path, top_k=20)
            search_titles = {r["title"] for r in search_results}
            search_scores = {r["title"]: r["score"] for r in search_results}

            topic_candidates = []
            for c in candidates:
                if c["title"] in search_titles:
                    c["priority"] *= 1 + search_scores[c["title"]]
                    topic_candidates.append(c)

            if topic_candidates:
                candidates = topic_candidates
        except Exception:
            logger.exception("Topic search failed, falling back to unfiltered selection")

    # Step 3: Weighted random selection from top 5
    candidates.sort(key=lambda c: c["priority"], reverse=True)
    top = candidates[:5]

    # Use priority as weight (minimum 0.1 to give all candidates a chance)
    weights = [max(c["priority"], 0.1) for c in top]
    selected = random.choices(top, weights=weights, k=1)[0]

    return {
        "path": selected["path"],
        "title": selected["title"],
        "priority": selected["priority"],
    }


# ---------------------------------------------------------------------------
# Note parsing
# ---------------------------------------------------------------------------


def parse_note_sections(note_path: Path) -> dict:
    """
    Extract key sections from a resource note.

    Returns {"why_saved": str, "takeaways": str, "tags": list[str], "source": str}.
    """
    try:
        content = note_path.read_text(errors="ignore")
    except OSError:
        return {"why_saved": "", "takeaways": "", "tags": [], "source": ""}

    # Extract frontmatter
    tags = []
    source = ""
    fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        tag_match = re.search(r"^tags:\s*\[([^\]]*)\]", fm, re.MULTILINE)
        if tag_match:
            tags = [t.strip().strip("\"'") for t in tag_match.group(1).split(",") if t.strip()]
        source_match = re.search(r"^source:\s*(.+)$", fm, re.MULTILINE)
        if source_match:
            source = source_match.group(1).strip()

    # Extract "Why I saved this" callout
    why_saved = ""
    why_match = re.search(
        r"> \[!note\][^\n]*\n((?:> .+\n?)+)", content
    )
    if why_match:
        why_lines = why_match.group(1).strip().split("\n")
        why_saved = " ".join(line.lstrip("> ").strip() for line in why_lines)

    # Extract Key Takeaways section
    takeaways = ""
    takeaway_match = re.search(
        r"## Key Takeaways\n((?:[-*] .+\n?)+)", content
    )
    if takeaway_match:
        takeaways = takeaway_match.group(1).strip()

    return {
        "why_saved": why_saved,
        "takeaways": takeaways,
        "tags": tags,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


def gather_revisit_context(
    note_path: Path,
    vault_path: str,
    invoke_claude_fn,
) -> dict:
    """
    Build the full context for a revisit prompt.

    Returns {"note_title": str, "why_saved": str, "takeaways": str,
             "related_notes": list[dict], "synthesis": str}.
    """
    note_title = note_path.stem
    sections = parse_note_sections(note_path)

    # Search for related notes
    search_query = note_title
    if sections["takeaways"]:
        search_query += " " + sections["takeaways"][:200]

    related_notes = []
    try:
        results = search_vault(search_query, vault_path, top_k=6)
        # Exclude the note itself
        results = [r for r in results if r["title"] != note_title][:3]

        for r in results:
            r_path = Path(vault_path) / r["path"]
            r_sections = parse_note_sections(r_path)
            related_notes.append({
                "title": r["title"],
                "score": r["score"],
                "takeaways": r_sections["takeaways"],
                "online_url": _resolve_online_url(r["title"]),
            })
    except Exception:
        logger.exception("Failed to search for related notes")

    # Generate 2-3 questions / provocations via Claude Haiku
    provocations = []
    if sections["takeaways"] or related_notes:
        provocations = _generate_provocations(
            note_title, sections["takeaways"], related_notes, invoke_claude_fn, vault_path
        )

    return {
        "note_title": note_title,
        "why_saved": sections["why_saved"],
        "takeaways": sections["takeaways"],
        "related_notes": related_notes,
        "provocations": provocations,
        "online_url": _resolve_online_url(note_title),
    }


def _resolve_online_url(title: str) -> str:
    """Resolve a note's GitHub Pages URL, or '' if Pages isn't configured / no slug."""
    if not (settings.pages_repo_path and settings.pages_base_url):
        return ""
    try:
        from pages import SlugMap
        slug_map = SlugMap(Path(settings.pages_repo_path))
        slug = slug_map.resolve(title)
        if slug:
            return f"{settings.pages_base_url.rstrip('/')}/{slug}.html"
    except Exception:
        pass
    return ""


def _generate_provocations(
    title: str,
    takeaways: str,
    related_notes: list[dict],
    invoke_claude_fn,
    vault_path: str,
) -> list[str]:
    """Generate 2-3 questions/provocations connecting the note to related notes."""
    template_path = PROMPTS_DIR / "revisit_provocations_prompt.md"
    if not template_path.exists():
        logger.warning("revisit_provocations_prompt.md not found")
        return []

    template = template_path.read_text()

    related_block = ""
    for rn in related_notes:
        related_block += f"\n### [[{rn['title']}]]\n"
        if rn["takeaways"]:
            related_block += f"{rn['takeaways']}\n"
        else:
            related_block += "(No takeaways available)\n"

    prompt = (
        template
        .replace("{title}", title)
        .replace("{takeaways}", takeaways or "(No takeaways recorded)")
        .replace("{related_notes_with_takeaways}", related_block or "(No related notes found)")
    )

    try:
        result = invoke_claude_fn(prompt, hash(title) & 0xFFFFFFFF, vault_path, model="claude-haiku-4-5-20251001")
    except Exception:
        logger.exception("Provocation generation failed")
        return []

    # One question per non-empty line; strip any bullet/number prefix the model added.
    questions = []
    for line in result.strip().splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line.strip())
        if line:
            questions.append(line)
    return questions[:3]


# ---------------------------------------------------------------------------
# Telegram message formatting
# ---------------------------------------------------------------------------


def format_revisit_telegram_message(context: dict) -> str:
    """Assemble the Telegram message for a revisit prompt."""
    header = f"Time to revisit: [[{context['note_title']}]]"
    if context.get("online_url"):
        header += f"\n{context['online_url']}"
    lines = [header]

    if context["why_saved"]:
        lines.append(f"\nWhy you saved this:\n> {context['why_saved']}")

    if context["takeaways"]:
        lines.append(f"\nKey takeaways:\n{context['takeaways']}")

    if context["related_notes"]:
        lines.append("\nConnected notes:")
        for rn in context["related_notes"]:
            if rn.get("online_url"):
                lines.append(f"- [[{rn['title']}]] — {rn['online_url']}")
            else:
                lines.append(f"- [[{rn['title']}]]")

    if context.get("provocations"):
        lines.append("\nQuestions to sit with:")
        for i, q in enumerate(context["provocations"], 1):
            lines.append(f"{i}. {q}")
    else:
        lines.append(
            "\nHow has your thinking on this evolved? "
            "What connections do you see to current work? "
            "What would you explore further?"
        )
    lines.append("\n(Reply to this message with text or a voice note)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context strategy wrapper (for scheduled chains)
# ---------------------------------------------------------------------------


def gather_revisit_context_for_chain(
    vault_path: str,
    state: dict,
    invoke_claude_fn,
) -> str:
    """
    Called by reflections.gather_context() for the "revisit_note" strategy.

    Selects a note and builds the formatted Telegram message.
    Stores the selected note filename in state for later recording.
    """
    selected = select_revisit_note(
        vault_path, settings.obsidian_resource_folder, state
    )
    if not selected:
        return "No notes available to revisit yet. Keep saving resources!"

    context = gather_revisit_context(selected["path"], vault_path, invoke_claude_fn)

    # Store selected note in state so reflections.py can transfer it to pending data
    state["pending_revisit_note"] = selected["path"].name
    state["pending_revisit_title"] = selected["title"]

    # Build clean context for the reflection prompt template
    context_parts = []
    if context.get("takeaways"):
        context_parts.append(context["takeaways"])
    if context.get("provocations"):
        context_parts.append(
            "\nQuestions raised:\n" + "\n".join(f"- {q}" for q in context["provocations"])
        )
    state["pending_revisit_context"] = "\n".join(context_parts)

    return format_revisit_telegram_message(context)


# ---------------------------------------------------------------------------
# /revisit command handler
# ---------------------------------------------------------------------------


def handle_revisit_command(
    text: str,
    chat_id: int,
    state: dict,
    send_message_fn,
    invoke_claude_fn,
) -> dict:
    """Handle the /revisit [topic] command. Returns updated state."""
    topic = text[len("/revisit"):].strip() or None

    send_message_fn(chat_id, "Looking for something to revisit...")

    vault_path = str(settings.obsidian_vault_path)

    selected = select_revisit_note(
        vault_path, settings.obsidian_resource_folder, state, topic=topic
    )
    if not selected:
        send_message_fn(chat_id, "No notes available to revisit yet. Keep saving resources!")
        return state

    context = gather_revisit_context(selected["path"], vault_path, invoke_claude_fn)
    message = format_revisit_telegram_message(context)

    result = send_message_fn(chat_id, message)
    msg_id = result.get("message_id") if isinstance(result, dict) else None

    if msg_id is None:
        logger.error("Could not capture message_id for revisit prompt")
        return state

    # Save as pending reflection so reply-to matching works.
    # Use "note-revisit-tue" chain ID — it shares the same prompt_template and folder
    # as "note-revisit-thu", so either works for on-demand use.
    chain_id = "note-revisit-tue"

    # Build context string for the reflection prompt template ({context} placeholder)
    context_parts = []
    if context.get("takeaways"):
        context_parts.append(context["takeaways"])
    if context.get("provocations"):
        context_parts.append(
            "\nQuestions raised:\n" + "\n".join(f"- {q}" for q in context["provocations"])
        )
    context_str = "\n".join(context_parts)

    save_pending(state, chain_id, msg_id, context_str)

    # Store extra data for revisit recording
    if "pending_reflections" in state and chain_id in state["pending_reflections"]:
        state["pending_reflections"][chain_id]["revisit_note_filename"] = selected["path"].name
        state["pending_reflections"][chain_id]["revisit_note_title"] = selected["title"]

    return state


# ---------------------------------------------------------------------------
# Revisit history tracking
# ---------------------------------------------------------------------------


def record_revisit(state: dict, note_filename: str) -> None:
    """Record that a note was revisited today."""
    if "revisit_history" not in state:
        state["revisit_history"] = {}

    today = datetime.now().strftime("%Y-%m-%d")
    history = state["revisit_history"].get(note_filename, {
        "revisit_dates": [],
        "revisit_count": 0,
    })

    history["revisit_dates"].append(today)
    history["revisit_count"] = len(history["revisit_dates"])
    state["revisit_history"][note_filename] = history
