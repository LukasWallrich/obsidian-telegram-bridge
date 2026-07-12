"""
kb_query.py — CLI for querying the Obsidian knowledge base by meaning.

Semantic search over the vault (bge-micro-v2 embeddings), printing the most
relevant notes with a short excerpt and — when GitHub Pages is configured — a
public link. Intended to be wrapped by ~/.local/bin/knowledge-base and called
by the OpenClaw/Codex agent (the "knowledge-base" skill).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import settings
from search import search_vault


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def _resolve_url(title: str) -> str:
    """GitHub Pages URL for a note, or '' if Pages isn't configured / no slug."""
    if not (settings.pages_repo_path and settings.pages_base_url):
        return ""
    try:
        from pages import SlugMap
        slug = SlugMap(Path(settings.pages_repo_path)).resolve(title)
        if slug:
            return f"{settings.pages_base_url.rstrip('/')}/{slug}.html"
    except Exception:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Semantic search over the Obsidian knowledge base."
    )
    ap.add_argument("query", nargs="+", help="question or topic")
    ap.add_argument("--top-k", type=int, default=5, help="number of notes to return")
    ap.add_argument(
        "--chars", type=int, default=800,
        help="excerpt characters per note (0 = titles/links only)",
    )
    args = ap.parse_args()

    query = " ".join(args.query).strip()
    if not query:
        print("usage: kb_query.py <question or topic>", file=sys.stderr)
        return 2

    vault = Path(settings.obsidian_vault_path)
    results = search_vault(query, str(vault), top_k=args.top_k)
    if not results:
        print("No matching notes found (the vault index may be empty).")
        return 0

    print(f"Top {len(results)} notes for: {query}\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}  (similarity {r['score']:.2f})")
        print(f"   path: {r['path']}")
        url = _resolve_url(r["title"])
        if url:
            print(f"   link: {url}")
        if args.chars > 0:
            try:
                body = _strip_frontmatter((vault / r["path"]).read_text(errors="ignore"))
            except OSError:
                body = ""
            if body:
                excerpt = " ".join(body.split())[: args.chars]
                print(f"   excerpt: {excerpt}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
