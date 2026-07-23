import pathlib
import fitz  # pymupdf
import chromadb

DOCS_DIR = pathlib.Path("rag_docs")
DB_DIR = "chroma_db"

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection("harness_docs")

docs, ids, metas = [], [], []

for path in DOCS_DIR.iterdir():
    if path.suffix.lower() == ".pdf":
        pdf = fitz.open(path)
        for page_num, page in enumerate(pdf, start=1):
            text = page.get_text().strip()
            if len(text) < 40:  # skip blank/cover pages
                continue
            docs.append(text)
            ids.append(f"{path.stem}-p{page_num}")
            metas.append({"source": path.name, "page": page_num})
        pdf.close()
        print(f"Read {path.name}")
    elif path.suffix.lower() in (".md", ".txt"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunk, count = "", 0
        for p in paras:
            if len(chunk) + len(p) > 1200 and chunk:
                count += 1
                docs.append(chunk)
                ids.append(f"{path.stem}-c{count}")
                metas.append({"source": path.name, "page": count})
                chunk = ""
            chunk = (chunk + "\n\n" + p).strip()
        if chunk:
            count += 1
            docs.append(chunk)
            ids.append(f"{path.stem}-c{count}")
            metas.append({"source": path.name, "page": count})
        print(f"Read {path.name}")

print(f"Embedding {len(docs)} chunks (first run downloads the embedding model)...")
collection.upsert(documents=docs, ids=ids, metadatas=metas)
print(f"Done. Collection now holds {collection.count()} chunks.")