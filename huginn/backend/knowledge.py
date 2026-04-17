"""
Huginn knowledge base — ChromaDB vector store.
Semantic search over dotfiles, personal notes, and any indexed content.
"""
import hashlib
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from config import Config

CHUNK_SIZE    = 400
CHUNK_OVERLAP = 80
TOP_K         = 4
MAX_DISTANCE  = 1.4   # L2 distance threshold — higher = more permissive

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"
DOTFILES_DIR  = Path.home() / "dotfiles"
INDEXABLE     = {".md", ".txt", ".py", ".qml", ".kdl", ".conf", ".vim",
                 ".toml", ".json", ".sh", ".zsh", ".bash", ".fish"}

_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=str(Config.data_dir / "chroma"))
        _collection = client.get_or_create_collection(
            "huginn_knowledge",
            embedding_function=DefaultEmbeddingFunction(),
        )
    return _collection


def _chunks(text: str, source: str) -> list[dict]:
    out, start, idx = [], 0, 0
    while start < len(text):
        end   = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end].strip()
        if chunk:
            cid = hashlib.sha256(f"{source}:{idx}".encode()).hexdigest()[:16]
            out.append({"id": cid, "text": chunk, "source": source, "idx": idx})
        start += CHUNK_SIZE - CHUNK_OVERLAP
        idx   += 1
    return out


def index_file(path: str | Path) -> int:
    p = Path(path).expanduser().resolve()
    if not p.is_file() or p.suffix not in INDEXABLE:
        return 0
    try:
        text = p.read_text(errors="replace").strip()
    except OSError:
        return 0
    if not text:
        return 0

    col    = _get_collection()
    source = str(p)
    chunks = _chunks(text, source)
    if not chunks:
        return 0

    try:
        col.delete(where={"source": source})
    except Exception:
        pass

    col.add(
        ids       = [c["id"]   for c in chunks],
        documents = [c["text"] for c in chunks],
        metadatas = [{"source": source, "idx": c["idx"]} for c in chunks],
    )
    return len(chunks)


def index_directory(directory: str | Path) -> tuple[int, int]:
    """Returns (files_indexed, chunks_total)."""
    d = Path(directory).expanduser().resolve()
    if not d.is_dir():
        return 0, 0
    files, total = 0, 0
    for p in d.rglob("*"):
        if not p.is_file() or p.suffix not in INDEXABLE:
            continue
        # Skip hidden dirs, node_modules, .git, __pycache__, uv cache
        if any(part.startswith((".", "__")) for part in p.relative_to(d).parts):
            continue
        n = index_file(p)
        if n:
            files += 1
            total += n
    return files, total


def query(text: str, n: int = TOP_K) -> list[dict]:
    col = _get_collection()
    count = col.count()
    if count == 0:
        return []
    try:
        results = col.query(query_texts=[text], n_results=min(n, count))
    except Exception:
        return []
    out = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        if dist < MAX_DISTANCE:
            source = Path(meta.get("source", "")).name
            out.append({"text": doc, "source": source, "distance": round(dist, 3)})
    return out


def total_chunks() -> int:
    return _get_collection().count()
