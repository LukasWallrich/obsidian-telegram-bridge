"""
reflections.py — Scheduled reflection chains for the Obsidian Telegram Resource Bridge.

Sends periodic reflection prompts via Telegram, collects voice/text responses,
and saves lightly-edited reflection notes to the vault.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "reflections" / "config.json"
PROMPTS_DIR = BASE_DIR / "prompts" / "reflections"

# ---------------------------------------------------------------------------
# Chain configuration
# ---------------------------------------------------------------------------


def load_chains() -> list[dict]:
    """Load reflection chain definitions from config.json."""
    if not CONFIG_FILE.exists():
        return []
    data = json.loads(CONFIG_FILE.read_text())
    return data.get("chains", [])


def save_chains(chains: list[dict]) -> None:
    """Write reflection chain definitions back to config.json."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"chains": chains}, indent=2) + "\n")


def find_chain(chain_id: str) -> dict | None:
    """Find a chain by its id."""
    for c in load_chains():
        if c["id"] == chain_id:
            return c
    return None


def _slugify(name: str) -> str:
    """Convert a chain name to a slug id."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


def gather_last_week_ahead(vault_path: str, reflections_folder: str) -> str:
    """Find the most recent week-ahead reflection from the last 7 days."""
    week_ahead_dir = Path(vault_path) / reflections_folder / "week-ahead"
    if not week_ahead_dir.exists():
        return ""

    today = datetime.now().date()
    cutoff = today - timedelta(days=7)

    # Look for files matching "YYYY-MM-DD Week Ahead.md"
    candidates = []
    for f in week_ahead_dir.glob("*.md"):
        # Extract date from filename
        match = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        if match:
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                if cutoff <= file_date <= today:
                    candidates.append((file_date, f))
            except ValueError:
                continue

    if not candidates:
        return ""

    # Pick the most recent
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best = candidates[0]

    content = best.read_text(errors="ignore")
    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    # Strip footer
    lines = content.split("\n")
    while lines and (lines[-1].strip().startswith("*") or lines[-1].strip() == ""):
        lines.pop()

    return f"Your goals from last Sunday:\n\n{chr(10).join(lines)}"


def gather_since_last_run(
    chain_id: str, vault_path: str, resource_folder: str, state: dict
) -> str:
    """Gather all resource notes saved since this chain's last run."""
    reflections_sent = state.get("reflections_sent", {})
    last_run_str = reflections_sent.get(chain_id)

    if last_run_str:
        try:
            cutoff = datetime.strptime(last_run_str, "%Y-%m-%d").date()
        except ValueError:
            cutoff = datetime.now().date() - timedelta(days=7)
    else:
        cutoff = datetime.now().date() - timedelta(days=7)

    resource_dir = Path(vault_path) / resource_folder
    if not resource_dir.exists():
        return ""

    # Load slug map for online links if pages are configured
    slug_map = None
    base_url = ""
    if settings.pages_repo_path and settings.pages_base_url:
        try:
            from pages import SlugMap
            slug_map = SlugMap(Path(settings.pages_repo_path))
            base_url = settings.pages_base_url.rstrip("/")
        except Exception:
            pass

    notes = []
    for f in sorted(resource_dir.glob("*.md")):
        match = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        if match:
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                if file_date > cutoff:
                    title = f.stem
                    # Read first non-frontmatter, non-empty line as a preview
                    content = f.read_text(errors="ignore")
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        if end != -1:
                            content = content[end + 3:].strip()
                    preview = ""
                    for line in content.split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#") and not line.startswith(">"):
                            preview = line[:120]
                            break
                    # Build online link if available
                    url = ""
                    if slug_map:
                        slug = slug_map.resolve(title)
                        if slug:
                            url = f"{base_url}/{slug}.html"
                    link = f" ({url})" if url else ""
                    notes.append(f"- [[{title}]]{link}: {preview}" if preview else f"- [[{title}]]{link}")
            except ValueError:
                continue

    if not notes:
        return ""

    return "\n".join(notes)


def gather_context(
    strategy: str, chain_id: str, vault_path: str, state: dict,
    invoke_claude_fn=None,
) -> str:
    """Dispatch to the appropriate context gathering function."""
    if strategy == "last_week_ahead":
        return gather_last_week_ahead(vault_path, settings.obsidian_reflections_folder)
    elif strategy == "since_last_run":
        return gather_since_last_run(
            chain_id, vault_path, settings.obsidian_resource_folder, state
        )
    elif strategy == "revisit_note":
        from revisit import gather_revisit_context_for_chain
        return gather_revisit_context_for_chain(vault_path, state, invoke_claude_fn)
    return ""


# ---------------------------------------------------------------------------
# Pending reflection management (stored in state.json, not separate files)
# ---------------------------------------------------------------------------


def save_pending(state: dict, chain_id: str, message_id: int, context: str) -> None:
    """Record a pending reflection in state dict."""
    if "pending_reflections" not in state:
        state["pending_reflections"] = {}
    state["pending_reflections"][chain_id] = {
        "chain_id": chain_id,
        "message_id": message_id,
        "context": context,
        "sent_at": datetime.now().isoformat(),
    }


def clear_pending(state: dict, chain_id: str) -> None:
    """Remove a pending reflection from state dict."""
    pending = state.get("pending_reflections", {})
    pending.pop(chain_id, None)


def match_pending_reflection(state: dict, reply_msg_id: int) -> dict | None:
    """Check if a reply-to message_id matches any pending reflection."""
    for data in state.get("pending_reflections", {}).values():
        if data.get("message_id") == reply_msg_id:
            return data
    return None


def expire_pending_reflections(state: dict, max_age_hours: int = 168) -> None:
    """Remove pending reflections older than max_age_hours from state."""
    pending = state.get("pending_reflections", {})
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    expired = []
    for chain_id, data in pending.items():
        try:
            sent_at = datetime.fromisoformat(data["sent_at"])
            if sent_at < cutoff:
                logger.info("Expiring pending reflection: %s", chain_id)
                expired.append(chain_id)
        except (KeyError, ValueError):
            expired.append(chain_id)
    for chain_id in expired:
        pending.pop(chain_id, None)


# ---------------------------------------------------------------------------
# Schedule checking
# ---------------------------------------------------------------------------


def check_due_reflections(state: dict) -> list[dict]:
    """Return chains that are due to be sent right now."""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    current_day = now.strftime("%A").lower()
    current_time = now.strftime("%H:%M")

    reflections_sent = state.get("reflections_sent", {})
    due = []

    for chain in load_chains():
        if not chain.get("active", True):
            continue
        if chain["schedule_day"] != current_day:
            continue
        if current_time < chain["schedule_time"]:
            continue
        # Already sent today?
        if reflections_sent.get(chain["id"]) == today_str:
            continue
        due.append(chain)

    return due


# ---------------------------------------------------------------------------
# Send reflection prompt
# ---------------------------------------------------------------------------


def send_reflection_prompt(
    chain: dict,
    chat_id: int,
    vault_path: str,
    state: dict,
    send_message_fn,
    invoke_claude_fn=None,
) -> None:
    """Gather context, send Telegram prompt, save pending state."""
    context = gather_context(
        chain.get("context_strategy", "none"),
        chain["id"],
        vault_path,
        state,
        invoke_claude_fn=invoke_claude_fn,
    )

    # Build telegram message
    telegram_prompt = chain["telegram_prompt"]
    if "{context_preview}" in telegram_prompt:
        if context:
            telegram_prompt = telegram_prompt.replace("{context_preview}", context)
        else:
            # Adapt message when no context available
            if chain["context_strategy"] == "last_week_ahead":
                telegram_prompt = telegram_prompt.replace(
                    "{context_preview}",
                    "(No week-ahead goals found from last Sunday)",
                )
            elif chain["context_strategy"] == "since_last_run":
                telegram_prompt = (
                    "Nothing saved since last time — anything on your mind "
                    "that you'd like to capture?\n\n"
                    "(Reply to this message with text or a voice note)"
                )
            else:
                telegram_prompt = telegram_prompt.replace("{context_preview}", "")

    # Send and capture message_id
    result = send_message_fn(chat_id, telegram_prompt)
    msg_id = result.get("message_id") if isinstance(result, dict) else None

    if msg_id is None:
        logger.error("Could not capture message_id for reflection prompt")
        return

    # For revisit chains, use the clean context (takeaways + synthesis) instead of
    # the full Telegram message as the {context} for the reflection prompt template.
    pending_context = context
    if chain.get("context_strategy") == "revisit_note":
        pending_context = state.pop("pending_revisit_context", context)

    save_pending(state, chain["id"], msg_id, pending_context)

    # For revisit chains, store the selected note filename and title for history tracking
    if chain.get("context_strategy") == "revisit_note":
        pending_revisit_note = state.pop("pending_revisit_note", None)
        pending_revisit_title = state.pop("pending_revisit_title", None)
        if pending_revisit_note and chain["id"] in state.get("pending_reflections", {}):
            state["pending_reflections"][chain["id"]]["revisit_note_filename"] = pending_revisit_note
            state["pending_reflections"][chain["id"]]["revisit_note_title"] = pending_revisit_title or ""

    logger.info("Sent reflection prompt for chain '%s' (msg_id=%s)", chain["id"], msg_id)


def check_and_send_reflections(
    state: dict, chat_id: int, send_message_fn, invoke_claude_fn=None,
) -> dict:
    """Check for due reflections and send them. Returns updated state."""
    expire_pending_reflections(state)

    due = check_due_reflections(state)
    if not due:
        return state

    vault_path = str(settings.obsidian_vault_path)

    for chain in due:
        try:
            send_reflection_prompt(
                chain, chat_id, vault_path, state, send_message_fn,
                invoke_claude_fn=invoke_claude_fn,
            )
            # Mark as sent today
            if "reflections_sent" not in state:
                state["reflections_sent"] = {}
            state["reflections_sent"][chain["id"]] = datetime.now().strftime("%Y-%m-%d")
        except Exception:
            logger.exception("Failed to send reflection for chain '%s'", chain["id"])

    return state


# ---------------------------------------------------------------------------
# Process reflection response
# ---------------------------------------------------------------------------


def process_reflection_response(
    pending: dict,
    user_text: str,
    chat_id: int,
    vault_path: str,
    invoke_claude_fn,
    send_message_fn,
    state: dict,
) -> None:
    """Build a Haiku prompt to lightly format the response, invoke, save note."""
    chain_id = pending["chain_id"]
    chain = find_chain(chain_id)
    if not chain:
        send_message_fn(chat_id, f"Reflection chain '{chain_id}' no longer exists.")
        clear_pending(state, chain_id)
        return

    # Load prompt template
    template_name = chain.get("prompt_template", "generic_prompt.md")
    template_path = PROMPTS_DIR / template_name
    if not template_path.exists():
        template_path = PROMPTS_DIR / "generic_prompt.md"

    template = template_path.read_text()
    today = datetime.now().strftime("%Y-%m-%d")

    # Fill template
    prompt = (
        template
        .replace("{user_response}", user_text)
        .replace("{context}", pending.get("context", ""))
        .replace("{date}", today)
        .replace("{reflections_folder}", settings.obsidian_reflections_folder)
        .replace("{folder}", chain.get("folder", chain_id))
        .replace("{chain_name}", chain.get("name", chain_id))
        .replace("{chain_id}", chain_id)
        .replace("{telegram_prompt}", chain.get("telegram_prompt", "").replace("{context_preview}", "").strip())
    )

    # Extra placeholders for revisit reflections
    revisit_title = pending.get("revisit_note_title", "")
    if revisit_title:
        # short_title strips the leading date prefix (e.g., "2026-01-15 ")
        short_title = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", revisit_title)
        prompt = (
            prompt
            .replace("{revisit_note_title}", revisit_title)
            .replace("{short_title}", short_title)
        )

    # Ensure the target directory exists
    target_dir = Path(vault_path) / settings.obsidian_reflections_folder / chain.get("folder", chain_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        stdout = invoke_claude_fn(prompt, hash(user_text) & 0xFFFFFFFF, vault_path, model="claude-haiku-4-5-20251001")
    except Exception as exc:
        logger.exception("Claude failed for reflection '%s'", chain_id)
        send_message_fn(chat_id, f"Failed to save reflection: {exc}")
        return

    # Parse SAVED: line
    filename = None
    for line in stdout.splitlines():
        if line.startswith("SAVED:"):
            filename = line.split(":", 1)[1].strip()
            break

    if filename:
        note_title = Path(filename).stem
        send_message_fn(chat_id, f"Reflection saved: [[{note_title}]]")
    else:
        logger.warning("Claude did not return SAVED: line for reflection '%s'. Output: %s", chain_id, stdout[:500])
        send_message_fn(chat_id, "Reflection processed but couldn't confirm the save. Check your vault.")

    clear_pending(state, chain_id)


# ---------------------------------------------------------------------------
# /reflect command handler
# ---------------------------------------------------------------------------


def handle_reflect_command(
    text: str,
    chat_id: int,
    state: dict,
    send_message_fn,
    invoke_claude_fn,
) -> dict:
    """Handle /reflect subcommands. Returns potentially updated state."""
    parts = text.strip().split(maxsplit=1)
    # Strip the "/reflect" prefix
    args = parts[1].strip() if len(parts) > 1 else ""
    sub_parts = args.split(maxsplit=1)
    subcommand = sub_parts[0].lower() if sub_parts else "list"
    sub_args = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    if subcommand == "list":
        return _cmd_list(chat_id, state, send_message_fn)

    elif subcommand == "pause":
        return _cmd_pause(sub_args, chat_id, state, send_message_fn)

    elif subcommand == "resume":
        return _cmd_pause(sub_args, chat_id, state, send_message_fn, resume=True)

    elif subcommand == "trigger":
        return _cmd_trigger(sub_args, chat_id, state, send_message_fn, invoke_claude)

    elif subcommand == "skip":
        return _cmd_skip(chat_id, state, send_message_fn)

    elif subcommand == "schedule":
        return _cmd_schedule(sub_args, chat_id, state, send_message_fn)

    elif subcommand == "add":
        return _cmd_add(sub_args, chat_id, state, send_message_fn)

    elif subcommand == "remove":
        return _cmd_remove(sub_args, chat_id, state, send_message_fn)

    else:
        send_message_fn(
            chat_id,
            "Unknown subcommand. Available:\n"
            "/reflect list — show all chains\n"
            "/reflect pause <id> — pause a chain\n"
            "/reflect resume <id> — resume a chain\n"
            "/reflect trigger <id> — trigger now\n"
            "/reflect skip — dismiss pending prompt\n"
            "/reflect schedule <id> <day> <HH:MM>\n"
            "/reflect add <name> <day> <HH:MM> <prompt>\n"
            "/reflect remove <id>",
        )
        return state


def _cmd_list(chat_id: int, state: dict, send_message_fn) -> dict:
    chains = load_chains()
    if not chains:
        send_message_fn(chat_id, "No reflection chains configured.")
        return state

    reflections_sent = state.get("reflections_sent", {})
    lines = ["Reflection chains:\n"]
    for c in chains:
        status = "active" if c.get("active", True) else "PAUSED"
        last = reflections_sent.get(c["id"], "never")
        lines.append(
            f"• {c['name']} ({c['id']})\n"
            f"  Schedule: {c['schedule_day'].title()} {c['schedule_time']}\n"
            f"  Status: {status} | Last sent: {last}"
        )

    # Show pending
    pending = state.get("pending_reflections", {})
    if pending:
        lines.append("\nPending responses:")
        for chain_id, data in pending.items():
            lines.append(f"  • {chain_id} (sent {data.get('sent_at', '?')[:16]})")

    send_message_fn(chat_id, "\n".join(lines))
    return state


def _cmd_pause(sub_args: str, chat_id: int, state: dict, send_message_fn, resume: bool = False) -> dict:
    chain_id = sub_args.strip()
    if not chain_id:
        send_message_fn(chat_id, f"Usage: /reflect {'resume' if resume else 'pause'} <chain-id>")
        return state

    chains = load_chains()
    found = False
    for c in chains:
        if c["id"] == chain_id:
            c["active"] = resume
            found = True
            break

    if not found:
        send_message_fn(chat_id, f"Chain '{chain_id}' not found.")
        return state

    save_chains(chains)
    action = "Resumed" if resume else "Paused"
    send_message_fn(chat_id, f"{action}: {chain_id}")
    return state


def _cmd_trigger(sub_args: str, chat_id: int, state: dict, send_message_fn, invoke_claude_fn=None) -> dict:
    chain_id = sub_args.strip()
    if not chain_id:
        send_message_fn(chat_id, "Usage: /reflect trigger <chain-id>")
        return state

    chain = find_chain(chain_id)
    if not chain:
        send_message_fn(chat_id, f"Chain '{chain_id}' not found.")
        return state

    vault_path = str(settings.obsidian_vault_path)
    try:
        send_reflection_prompt(chain, chat_id, vault_path, state, send_message_fn, invoke_claude_fn=invoke_claude_fn)
        if "reflections_sent" not in state:
            state["reflections_sent"] = {}
        state["reflections_sent"][chain_id] = datetime.now().strftime("%Y-%m-%d")
    except Exception as exc:
        logger.exception("Failed to trigger reflection '%s'", chain_id)
        send_message_fn(chat_id, f"Failed: {exc}")

    return state


def _cmd_skip(chat_id: int, state: dict, send_message_fn) -> dict:
    """Dismiss all pending reflections."""
    pending = state.get("pending_reflections", {})
    if not pending:
        send_message_fn(chat_id, "No pending reflections to skip.")
        return state

    skipped = list(pending.keys())
    state["pending_reflections"] = {}

    send_message_fn(chat_id, f"Skipped: {', '.join(skipped)}")
    return state


def _cmd_schedule(sub_args: str, chat_id: int, state: dict, send_message_fn) -> dict:
    # Expected: <chain-id> <day> <HH:MM>
    parts = sub_args.split()
    if len(parts) < 3:
        send_message_fn(chat_id, "Usage: /reflect schedule <chain-id> <day> <HH:MM>")
        return state

    chain_id, day, time_str = parts[0], parts[1].lower(), parts[2]

    valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    if day not in valid_days:
        send_message_fn(chat_id, f"Invalid day. Use one of: {', '.join(sorted(valid_days))}")
        return state

    if not re.match(r"^\d{2}:\d{2}$", time_str):
        send_message_fn(chat_id, "Time must be in HH:MM format (e.g. 18:00)")
        return state

    chains = load_chains()
    found = False
    for c in chains:
        if c["id"] == chain_id:
            c["schedule_day"] = day
            c["schedule_time"] = time_str
            found = True
            break

    if not found:
        send_message_fn(chat_id, f"Chain '{chain_id}' not found.")
        return state

    save_chains(chains)
    send_message_fn(chat_id, f"Rescheduled {chain_id}: {day.title()} {time_str}")
    return state


def _cmd_add(sub_args: str, chat_id: int, state: dict, send_message_fn) -> dict:
    # Expected: <name> <day> <HH:MM> <prompt text...>
    parts = sub_args.split(maxsplit=3)
    if len(parts) < 4:
        send_message_fn(chat_id, "Usage: /reflect add <name> <day> <HH:MM> <prompt text>")
        return state

    name, day, time_str, prompt_text = parts[0], parts[1].lower(), parts[2], parts[3]
    chain_id = _slugify(name)

    valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    if day not in valid_days:
        send_message_fn(chat_id, f"Invalid day. Use one of: {', '.join(sorted(valid_days))}")
        return state

    if not re.match(r"^\d{2}:\d{2}$", time_str):
        send_message_fn(chat_id, "Time must be in HH:MM format")
        return state

    # Check for duplicate
    if find_chain(chain_id):
        send_message_fn(chat_id, f"Chain '{chain_id}' already exists.")
        return state

    new_chain = {
        "id": chain_id,
        "name": name.replace("-", " ").replace("_", " ").title(),
        "schedule_day": day,
        "schedule_time": time_str,
        "prompt_template": "generic_prompt.md",
        "telegram_prompt": prompt_text + "\n\n(Reply to this message with text or a voice note)",
        "folder": chain_id,
        "context_strategy": "none",
        "active": True,
    }

    chains = load_chains()
    chains.append(new_chain)
    save_chains(chains)
    send_message_fn(chat_id, f"Added chain '{chain_id}': {day.title()} {time_str}")
    return state


def _cmd_remove(sub_args: str, chat_id: int, state: dict, send_message_fn) -> dict:
    chain_id = sub_args.strip()
    if not chain_id:
        send_message_fn(chat_id, "Usage: /reflect remove <chain-id>")
        return state

    chains = load_chains()
    new_chains = [c for c in chains if c["id"] != chain_id]

    if len(new_chains) == len(chains):
        send_message_fn(chat_id, f"Chain '{chain_id}' not found.")
        return state

    save_chains(new_chains)
    clear_pending(state, chain_id)
    send_message_fn(chat_id, f"Removed: {chain_id}")
    return state
