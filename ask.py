import sys
import chromadb

client = chromadb.PersistentClient(path="chroma_db")
collection = client.get_collection("harness_docs")

query = " ".join(sys.argv[1:]) or input("Question: ")

results = collection.query(query_texts=[query], n_results=3)

for doc, meta, dist in zip(
    results["documents"][0], results["metadatas"][0], results["distances"][0]
):
    print(f"\n--- {meta['source']} (page {meta['page']}, distance {dist:.3f}) ---")
    print(doc[:600])