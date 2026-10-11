"""Nạp tài liệu vào ChromaDB.

Luồng xử lý: đọc file trong data/ → chia đoạn (chunk) → tạo embedding bằng Gemini → lưu vào chroma_db/.

Cách chạy (từ thư mục gốc project):
    .venv/bin/python app/ingest.py            # nạp/cập nhật toàn bộ file trong data/
    .venv/bin/python app/ingest.py --reset    # xoá collection cũ rồi nạp lại từ đầu
"""

import argparse
import hashlib
import re
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pypdf import PdfReader
from tenacity import retry, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_DIR = ROOT / "chroma_db"

COLLECTION = "vn_docs"
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
EMBED_BATCH = 100       # số đoạn gửi trong một lần gọi API embedding

CHUNK_SIZE = 1000       # độ dài tối đa mỗi đoạn (ký tự)
CHUNK_OVERLAP = 200     # số ký tự lặp lại giữa hai đoạn liền kề để không mất ngữ cảnh

SUPPORTED = {".pdf", ".txt", ".md"}


# ---------- Đọc file ----------

def read_file(path: Path) -> list[tuple[int, str]]:
    """Trả về danh sách (số trang, nội dung). File text coi như chỉ có 1 trang."""
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(path)
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    return [(1, path.read_text(encoding="utf-8"))]


def clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- Chia đoạn ----------

def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Chia văn bản thành các đoạn <= size ký tự, ưu tiên cắt ở ranh giới đoạn văn / câu."""
    if len(text) <= size:
        return [text] if text else []

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            # Tìm điểm cắt đẹp nhất trong nửa sau của cửa sổ
            for sep in ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "):
                cut = window.rfind(sep, size // 2)
                if cut != -1:
                    end = start + cut + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---------- Embedding ----------

@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=60))
def embed_batch(client: genai.Client, texts: list[str]) -> list[list[float]]:
    resp = client.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_DOCUMENT",
            output_dimensionality=EMBED_DIM,
        ),
    )
    return [e.values for e in resp.embeddings]


def embed_all(client: genai.Client, texts: list[str]) -> list[list[float]]:
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(embed_batch(client, texts[i : i + EMBED_BATCH]))
    return vectors


# ---------- Nạp vào ChromaDB ----------

def chunk_id(source: str, page: int, idx: int, text: str) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"{source}::p{page}::c{idx}::{digest}"


def ingest_file(path: Path, client: genai.Client, collection) -> int:
    source = path.relative_to(DATA_DIR).as_posix()

    ids, docs, metas = [], [], []
    for page, raw in read_file(path):
        for idx, chunk in enumerate(split_text(clean_text(raw))):
            ids.append(chunk_id(source, page, idx, chunk))
            docs.append(chunk)
            metas.append({"source": source, "page": page, "chunk": idx})

    # Xoá các đoạn cũ của file này để khi sửa file thì không còn dữ liệu thừa
    collection.delete(where={"source": source})

    if not docs:
        print(f"  ! {source}: không trích được chữ nào (có thể là PDF dạng ảnh scan)")
        return 0

    vectors = embed_all(client, docs)
    collection.upsert(ids=ids, documents=docs, embeddings=vectors, metadatas=metas)
    return len(docs)


def get_collection(reset: bool = False):
    db = chromadb.PersistentClient(path=str(DB_DIR))
    if reset:
        try:
            db.delete_collection(COLLECTION)
        except Exception:
            pass
    return db.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})


def main():
    parser = argparse.ArgumentParser(description="Nạp tài liệu trong data/ vào ChromaDB")
    parser.add_argument("--reset", action="store_true", help="xoá toàn bộ dữ liệu cũ trước khi nạp")
    args = parser.parse_args()

    files = sorted(p for p in DATA_DIR.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
    if not files:
        print(f"Không có file nào ({', '.join(sorted(SUPPORTED))}) trong {DATA_DIR}")
        return

    load_dotenv(ROOT / ".env")
    client = genai.Client()
    collection = get_collection(reset=args.reset)

    total = 0
    for path in files:
        print(f"- Đang xử lý {path.name} ...")
        n = ingest_file(path, client, collection)
        print(f"  ✓ {n} đoạn")
        total += n

    print(f"\nXong: {len(files)} file, {total} đoạn. Collection '{COLLECTION}' hiện có {collection.count()} đoạn.")


if __name__ == "__main__":
    main()
