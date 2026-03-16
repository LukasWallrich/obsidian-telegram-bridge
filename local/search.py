"""
search.py — Semantic search over Obsidian vault using Smart Connections embeddings.

Uses the same TaylorAI/bge-micro-v2 (384-dim) ONNX model that Smart Connections
maintains, so queries are directly comparable to stored note embeddings.

Dependencies: onnxruntime, tokenizers
"""

import json
import logging
import math
import re
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
MODEL_CACHE = BASE_DIR / "local" / "models"

# ---------------------------------------------------------------------------
# AJSON parsing
# ---------------------------------------------------------------------------


def parse_ajson_entries(line: str) -> list[tuple[str, dict]]:
    """
    Parse an AJSON line which may contain multiple entries.
    Each entry is: "key": {json_object},
    Multiple entries can be concatenated on a single line.
    Returns list of (key, object) tuples.
    """
    line = line.strip()
    if not line:
        return []
    results = []
    # Split on entry boundaries: },"key": pattern
    # We wrap the whole line in { } to make it valid JSON
    try:
        wrapped = "{" + line.rstrip(",") + "}"
        data = json.loads(wrapped)
        for key, obj in data.items():
            results.append((key, obj))
    except json.JSONDecodeError:
        pass
    return results


def load_source_embeddings(vault_path: str) -> tuple[list[dict], str]:
    """
    Load SmartSource embeddings from Smart Connections .ajson files.

    Returns:
        (entries, model_name) where entries is a list of
        {"path": str, "title": str, "vec": list[float]}
    """
    vault = Path(vault_path)
    smart_env_config = vault / ".smart-env" / "smart_env.json"
    if not smart_env_config.exists():
        logger.warning("smart_env.json not found at %s", smart_env_config)
        return [], ""

    config = json.loads(smart_env_config.read_text())
    model_name = (
        config.get("smart_sources", {})
        .get("embed_model", {})
        .get("transformers", {})
        .get("model_key", "")
    )
    if not model_name:
        logger.warning("No embedding model_key found in smart_env.json")
        return [], ""

    multi_dir = vault / ".smart-env" / "multi"
    if not multi_dir.exists():
        logger.warning("Smart Connections multi/ directory not found")
        return [], model_name

    entries = []
    for ajson_file in multi_dir.glob("*.ajson"):
        text = ajson_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            for _key, obj in parse_ajson_entries(line):
                if obj.get("class_name") != "SmartSource":
                    continue
                path = obj.get("path")
                if not path:
                    continue
                vec = (
                    obj.get("embeddings", {})
                    .get(model_name, {})
                    .get("vec")
                )
                if not vec:
                    continue
                title = Path(path).stem
                entries.append({"path": path, "title": title, "vec": vec})

    logger.info("Loaded %d source embeddings from Smart Connections", len(entries))
    return entries, model_name


# ---------------------------------------------------------------------------
# ONNX model management
# ---------------------------------------------------------------------------

HF_BASE = "https://huggingface.co"


def _download_file(url: str, dest: Path) -> None:
    """Download a file from URL to dest, creating parent dirs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s → %s", url, dest)
    urllib.request.urlretrieve(url, str(dest))


def ensure_onnx_model(model_name: str) -> Path:
    """
    Ensure ONNX model + tokenizer are cached locally.
    Downloads from HuggingFace on first use.
    Returns the model directory path.
    """
    model_dir = MODEL_CACHE / model_name.replace("/", "__")
    model_file = model_dir / "model.onnx"
    tokenizer_file = model_dir / "tokenizer.json"

    if model_file.exists() and tokenizer_file.exists():
        return model_dir

    # Download from HuggingFace
    repo_url = f"{HF_BASE}/{model_name}/resolve/main"
    if not model_file.exists():
        _download_file(f"{repo_url}/onnx/model.onnx", model_file)
    if not tokenizer_file.exists():
        _download_file(f"{repo_url}/tokenizer.json", tokenizer_file)

    return model_dir


# ---------------------------------------------------------------------------
# Query embedding
# ---------------------------------------------------------------------------


def embed_query(query: str, model_name: str) -> list[float]:
    """
    Tokenize query and run ONNX inference to get a 384-dim embedding.
    Uses mean pooling over token embeddings (matching bge-micro-v2 behaviour).
    """
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    model_dir = ensure_onnx_model(model_name)

    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding(length=512)

    encoded = tokenizer.encode(query)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

    session = ort.InferenceSession(str(model_dir / "model.onnx"))
    input_names = {inp.name for inp in session.get_inputs()}

    feeds = {"input_ids": input_ids, "attention_mask": attention_mask}
    if "token_type_ids" in input_names:
        feeds["token_type_ids"] = token_type_ids

    outputs = session.run(None, feeds)
    # outputs[0] is token_embeddings: shape (1, seq_len, hidden_dim)
    token_embeddings = outputs[0]

    # Mean pooling with attention mask
    mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
    summed = (token_embeddings * mask_expanded).sum(axis=1)
    count = mask_expanded.sum(axis=1).clip(min=1e-9)
    mean_pooled = summed / count

    vec = mean_pooled[0].tolist()
    return vec


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors using stdlib math."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Tag collection
# ---------------------------------------------------------------------------


def collect_vault_tags(vault_path: str) -> list[str]:
    """
    Scan vault notes for frontmatter tags and return a sorted unique list.
    Strips leading '#' and deduplicates.
    """
    vault = Path(vault_path)
    tags: set[str] = set()
    for md in vault.rglob("*.md"):
        if ".smart-env" in md.parts or ".obsidian" in md.parts:
            continue
        try:
            text = md.read_text(errors="ignore")
        except OSError:
            continue
        fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm:
            continue
        tm = re.search(r"^tags:\s*\[([^\]]*)\]", fm.group(1), re.MULTILINE)
        if not tm:
            continue
        for t in tm.group(1).split(","):
            t = t.strip().strip("\"'").lstrip("#")
            if t:
                tags.add(t)
    return sorted(tags)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def search_vault(query: str, vault_path: str, top_k: int = 10) -> list[dict]:
    """
    Semantic search over vault notes using Smart Connections embeddings.

    Returns top-k results as [{"title": str, "path": str, "score": float}],
    sorted by descending similarity.
    """
    entries, model_name = load_source_embeddings(vault_path)
    if not entries or not model_name:
        logger.warning("No embeddings available for search")
        return []

    query_vec = embed_query(query, model_name)

    scored = []
    for entry in entries:
        score = cosine_similarity(query_vec, entry["vec"])
        scored.append({
            "title": entry["title"],
            "path": entry["path"],
            "score": round(score, 4),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
