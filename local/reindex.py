"""
reindex.py — Headless semantic re-embedding of the Obsidian vault.

Smart Connections only re-embeds notes while Obsidian is open on the desktop.
This user works headless (Telegram bridge), so that index goes stale. This
script rebuilds a fresh note-level index (`local/vault_index.json`) with the
SAME bge-micro-v2 model + mean pooling that search.py uses for queries, so the
bridge's related-notes lookup and the obsidian-search skill stay current
without Obsidian.

Incremental: only embeds notes whose content hash changed since last run.
Run on a schedule (see com.user.obsidian-reindex.plist) or manually:

    .venv/bin/python local/reindex.py [--full] [--vault PATH]

Dependencies: onnxruntime, tokenizers, numpy (same as search.py).
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import search  # noqa: E402  (get_embedder, DEFAULT_MODEL, VAULT_INDEX_PATH)

logger = logging.getLogger("reindex")

MIN_CHARS = 200  # matches Smart Connections smart_sources.min_chars
EXCLUDED_DIRS = {
    ".smart-env", ".obsidian", ".trash", ".git", ".keyflow",
    ".stversions", ".stfolder", "Attachments", "node_modules",
}
FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def model_name_for(vault: Path) -> str:
    cfg = vault / ".smart-env" / "smart_env.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
            mk = (data.get("smart_sources", {}).get("embed_model", {})
                  .get("transformers", {}).get("model_key"))
            if mk:
                return mk
        except (json.JSONDecodeError, OSError):
            pass
    return search.DEFAULT_MODEL


def iter_notes(vault: Path):
    """Yield curated markdown notes (relative path, text), skipping excluded dirs."""
    for md in vault.rglob("*.md"):
        rel_parts = md.relative_to(vault).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts[:-1]):
            continue
        if md.stem == "Untitled":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        yield md.relative_to(vault).as_posix(), text


def embed_text(text: str) -> str:
    """Strip frontmatter and prepend the body; return what we feed the model."""
    body = FRONTMATTER_RE.sub("", text, count=1).strip()
    return body


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=None, help="vault path (default: from .env settings)")
    ap.add_argument("--full", action="store_true", help="re-embed everything, ignore cache")
    args = ap.parse_args()

    if args.vault:
        vault = Path(args.vault).expanduser()
    else:
        from config import settings
        vault = Path(settings.obsidian_vault_path).expanduser()

    model = model_name_for(vault)
    index_path = search.VAULT_INDEX_PATH

    old = {}
    if index_path.exists() and not args.full:
        try:
            old = json.loads(index_path.read_text()).get("notes", {})
        except (json.JSONDecodeError, OSError):
            old = {}

    embed = None  # lazily loaded so an unchanged run never loads the model
    notes_out: dict = {}
    seen = set()
    embedded = reused = 0

    for rel, text in iter_notes(vault):
        seen.add(rel)
        body = embed_text(text)
        if len(body) < MIN_CHARS:
            continue
        title = Path(rel).stem
        # Prepend the title so the note's own name informs its vector.
        payload = f"{title}\n\n{body}"
        h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

        prev = old.get(rel)
        if prev and prev.get("hash") == h and prev.get("model") == model and prev.get("vec"):
            notes_out[rel] = prev
            reused += 1
            continue

        if embed is None:
            logger.info("Loading %s ...", model)
            embed = search.get_embedder(model)
        notes_out[rel] = {"hash": h, "model": model, "vec": embed(payload)}
        embedded += 1

    pruned = len([p for p in old if p not in seen])
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "model": model,
        "updated_at": int(time.time() * 1000),
        "vault": str(vault),
        "notes": notes_out,
    }))
    tmp.replace(index_path)

    logger.info("Indexed %d notes (%d embedded, %d reused, %d pruned) → %s",
                len(notes_out), embedded, reused, pruned, index_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
