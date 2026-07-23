"""Manual-lookup tool for the harness agent layer.

Retrieves relevant passages from the indexed docs in rag_docs/
(TACDE Su-27 tactics manual + harness INSTALL doc). Intended to be
registered as an agent tool so the pilot model can consult doctrine
mid-flight, e.g. missile employment ranges or evasion procedures.
"""
import chromadb

_client = chromadb.PersistentClient(path="chroma_db")
_collection = _client.get_collection("harness_docs")

MAX_DISTANCE = 1.1  # beyond this, treat as "not covered by the docs"


def search_manual(query: str, k: int = 3) -> str:
    """Return the top-k relevant doc passages for `query`, with citations.

    Returns a formatted string ready to inject into agent context.
    Says so explicitly when nothing relevant is found, rather than
    returning weak matches.
    """
    res = _collection.query(query_texts=[query], n_results=k)
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