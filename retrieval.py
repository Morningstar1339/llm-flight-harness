"""Manual-lookup tool for the harness agent layer.

Retrieves relevant passages from the indexed docs in rag_docs/
(TACDE Su-27 tactics manual + harness INSTALL doc). Intended to be
registered as an agent tool so the pilot model can consult doctrine
mid-flight, e.g. missile employment ranges or evasion procedures.

Plumbing notes (7/25):
  * The Chroma client is opened LAZILY, on first query. Opening it at
    import time meant importing this module could raise, and the daemon
    imports it through the tool registry — a missing index would have
    taken down the whole daemon at boot.
  * The store path is anchored to THIS FILE's directory, not the process
    CWD. The daemon runs from phase2/; the index lives at the repo root.
    A relative "chroma_db" resolved to phase2/chroma_db and silently
    missed.
  * chroma_db/ is gitignored (the indexed manual is copyrighted), so a
    fresh clone has no index. That is a normal, expected state: every
    entry point degrades to an "unavailable" report instead of raising.
    Rebuild with `python ingest.py`.

MAX_DISTANCE and the citation format are deliberately unchanged.
"""
from __future__ import annotations

import os
import threading

MAX_DISTANCE = 1.1  # beyond this, treat as "not covered by the docs"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CHROMA_PATH = os.path.join(REPO_ROOT, "chroma_db")
COLLECTION_NAME = "harness_docs"

UNAVAILABLE_PREFIX = "MANUAL UNAVAILABLE"


class ManualUnavailable(RuntimeError):
    """The vector store is absent or unreadable on this box.

    Not a bug: chroma_db/ is gitignored. Callers report and carry on.
    """


_lock = threading.Lock()
_collection = None


def _get_collection():
    """Open (once) and return the Chroma collection.

    Raises ManualUnavailable if the store cannot be opened. The failure
    is NOT cached — running `ingest.py` while the daemon is up makes the
    tool work on the next call without a restart.
    """
    global _collection
    with _lock:
        if _collection is not None:
            return _collection
        try:
            if not os.path.isdir(CHROMA_PATH):
                raise FileNotFoundError(
                    f"no vector store at {CHROMA_PATH} — run `python ingest.py`")
            import chromadb
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            _collection = client.get_collection(COLLECTION_NAME)
        except ManualUnavailable:
            raise
        except Exception as e:
            raise ManualUnavailable(f"{type(e).__name__}: {e}") from e
        return _collection


def manual_status() -> tuple[bool, str]:
    """(available, reason) — cheap probe for the tool registry / REPL."""
    try:
        _get_collection()
    except ManualUnavailable as e:
        return False, str(e)
    return True, f"{COLLECTION_NAME} @ {CHROMA_PATH}"


def manual_available() -> bool:
    return manual_status()[0]


def search_manual(query: str, k: int = 3) -> str:
    """Return the top-k relevant doc passages for `query`, with citations.

    Returns a formatted string ready to inject into agent context.
    Says so explicitly when nothing relevant is found, rather than
    returning weak matches, and when the index is unavailable, rather
    than raising into a caller that may be flying an aircraft.
    """
    try:
        collection = _get_collection()
    except ManualUnavailable as e:
        return f"{UNAVAILABLE_PREFIX}: {e}"
    res = collection.query(query_texts=[query], n_results=k)
    hits = [
        (doc, meta, dist)
        for doc, meta, dist in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        )
        if dist <= MAX_DISTANCE
    ]
    if not hits:
        return "MANUAL: no relevant passage found for this query."
    out = []
    for doc, meta, dist in hits:
        out.append(f"[{meta['source']} p.{meta['page']}] {doc.strip()}")
    return "\n\n".join(out)


if __name__ == "__main__":
    import sys
    print(search_manual(" ".join(sys.argv[1:]) or input("Question: ")))
