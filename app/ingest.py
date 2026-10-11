"""Nạp tài liệu vào Supabase.

Luồng xử lý: đọc file trong data/ → chia đoạn theo Chương/Mục/Điều (văn bản luật) hoặc theo trang
→ tạo embedding bằng Gemini → upload file gốc lên Storage → ghi các đoạn vào bảng chunks.
File không thay đổi (cùng checksum) sẽ được bỏ qua.

Cách chạy (từ thư mục gốc project):
    .venv/bin/python app/ingest.py            # nạp file mới hoặc file đã sửa
    .venv/bin/python app/ingest.py --force    # nạp lại tất cả, kể cả file không đổi
    .venv/bin/python app/ingest.py --prune    # xoá khỏi Supabase các tài liệu không còn trong data/
"""

import argparse
import hashlib
import re
import unicodedata
from bisect import bisect_right
from pathlib import Path

from pypdf import PdfReader

try:
    from app import store
    from app.core import DATA_DIR, embed
except ImportError:  # chạy trực tiếp: python app/ingest.py
    import store
    from core import DATA_DIR, embed

CHUNK_SIZE = 1500       # độ dài tối đa phần nội dung mỗi đoạn (ký tự)
CHUNK_OVERLAP = 200     # số ký tự lặp lại giữa hai đoạn liền kề khi một Điều quá dài

CONTENT_TYPES = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown"}
SUPPORTED = set(CONTENT_TYPES)

CHUONG_RE = re.compile(r"^Chương\s+([IVXLCDM]+|\d+)\s*$", re.MULTILINE)
MUC_RE = re.compile(r"^Mục\s+(\d+)\s*$", re.MULTILINE)
# "Điều 12. Quyền ..." ở đầu dòng. Bắt buộc có dấu chấm ngay sau số để không nhầm với
# câu dẫn chiếu bị xuống dòng như "Điều 22 của Luật này".
DIEU_RE = re.compile(r"^Điều\s+(\d+)\.[^\n]*", re.MULTILINE)
# Tên văn bản ở phần mở đầu: dòng "LUẬT" / "NGHỊ ĐỊNH" ... và dòng tên ngay bên dưới
DOC_TYPE_RE = re.compile(r"^(BỘ LUẬT|LUẬT|NGHỊ ĐỊNH|NGHỊ QUYẾT|THÔNG TƯ|QUYẾT ĐỊNH|PHÁP LỆNH)\s*$", re.MULTILINE)
DOC_NUMBER_RE = re.compile(r"^(?:Luật|Bộ luật|Nghị định|Nghị quyết|Thông tư|Quyết định|Pháp lệnh)? *số:?\s*([\w/.-]+)", re.MULTILINE | re.IGNORECASE)


# ---------- Đọc file ----------

def read_pages(path: Path) -> list[tuple[int, str]]:
    """Trả về danh sách (số trang, nội dung đã làm sạch). File text coi như chỉ có 1 trang."""
    if path.suffix.lower() == ".pdf":
        pages = [(i + 1, page.extract_text() or "") for i, page in enumerate(PdfReader(path).pages)]
    else:
        pages = [(1, path.read_text(encoding="utf-8"))]
    return [(n, clean_text(t)) for n, t in pages]


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\x00", ""))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"^ +| +$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d{1,3}$", "", text, flags=re.MULTILINE)   # dòng chỉ có số trang
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- Chia đoạn ----------

def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, str]]:
    """Chia văn bản thành các đoạn <= size ký tự, ưu tiên cắt ở ranh giới đoạn văn / câu.

    Trả về danh sách (vị trí bắt đầu trong text, nội dung đoạn).
    """
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "):
                cut = window.rfind(sep, size // 2)
                if cut != -1:
                    end = start + cut + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((start, chunk))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def next_line(text: str, pos: int) -> str:
    """Dòng không rỗng đầu tiên kể từ vị trí pos (tiêu đề Chương/Mục nằm ở dòng kế tiếp)."""
    for line in text[pos:].splitlines():
        if line.strip():
            return line.strip()
    return ""


def document_title(text: str) -> str | None:
    """Tên văn bản luật, vd "Luật viên chức số 129/2025/QH15". Gắn vào mọi đoạn để Gemini biết đoạn thuộc văn bản nào."""
    head = text[:3000]
    m = DOC_TYPE_RE.search(head)
    if not m:
        return None
    name = f"{m.group(1)} {next_line(head, m.end())}".strip()   # "LUẬT VIÊN CHỨC"
    title = name[0] + name[1:].lower()                              # "Luật viên chức"
    num = DOC_NUMBER_RE.search(head)
    return f"{title} số {num.group(1)}" if num else title


def split_legal(text: str) -> list[dict] | None:
    """Chia văn bản luật theo Điều. Trả về None nếu không phải văn bản luật.

    Mỗi phần: {start, end, chuong, muc, dieu, title}. Tiêu đề Chương/Mục được gắn vào các Điều
    bên dưới làm ngữ cảnh, không thành đoạn riêng.
    """
    headings = []
    for m in CHUONG_RE.finditer(text):
        headings.append((m.start(), "chuong", f"{m.group(0).strip()}. {next_line(text, m.end())}"))
    for m in MUC_RE.finditer(text):
        headings.append((m.start(), "muc", f"{m.group(0).strip()}. {next_line(text, m.end())}"))
    last = None
    for m in DIEU_RE.finditer(text):
        n = int(m.group(1))
        if last is None or last < n <= last + 5:   # số Điều phải tăng dần, loại dẫn chiếu lạc
            headings.append((m.start(), "dieu", (n, m.group(0).strip())))
            last = n
    if not any(kind == "dieu" for _, kind, _ in headings):
        return None

    headings.sort(key=lambda h: h[0])
    sections = []
    if text[: headings[0][0]].strip():   # phần mở đầu: tên luật, căn cứ ban hành
        sections.append({"start": 0, "end": headings[0][0], "chuong": None, "muc": None, "dieu": None, "title": None})

    chuong = muc = None
    for i, (pos, kind, value) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        if kind == "chuong":
            chuong, muc = value, None
        elif kind == "muc":
            muc = value
        else:
            dieu, title = value
            sections.append({"start": pos, "end": end, "chuong": chuong, "muc": muc, "dieu": dieu, "title": title})
    return sections


def build_chunks(pages: list[tuple[int, str]]) -> list[dict]:
    """Ghép các trang rồi chia đoạn. Mỗi đoạn: {chunk_index, chuong, dieu, page, content}."""
    starts, parts, offset = [], [], 0
    for _, page_text in pages:
        starts.append(offset)
        parts.append(page_text)
        offset += len(page_text) + 1
    text = "\n".join(parts)

    def page_at(pos: int) -> int:
        return pages[bisect_right(starts, pos) - 1][0]

    sections = split_legal(text)
    doc_title = document_title(text) if sections is not None else None
    if sections is None:   # tài liệu thường: chia theo từng trang
        sections = [
            {"start": s, "end": s + len(t), "chuong": None, "muc": None, "dieu": None, "title": None}
            for s, (_, t) in zip(starts, pages)
        ]

    chunks = []
    for sec in sections:
        body = text[sec["start"] : sec["end"]]
        breadcrumb = " › ".join(x for x in (doc_title, sec["chuong"], sec["muc"]) if x)
        for j, (rel, piece) in enumerate(split_text(body)):
            header = [breadcrumb] if breadcrumb else []
            if j > 0 and sec["title"]:
                header.append(f"{sec['title']} (tiếp)")
            chunks.append({
                "chunk_index": len(chunks),
                "chuong": sec["chuong"],
                "dieu": sec["dieu"],
                "page": page_at(sec["start"] + rel),
                "content": "\n".join(header + [piece]),
            })
    return chunks


# ---------- Nạp vào Supabase ----------

def ingest_file(path: Path, force: bool = False) -> str:
    name = path.relative_to(DATA_DIR).as_posix()
    data = path.read_bytes()
    checksum = hashlib.md5(data).hexdigest()

    existing = store.find_document(name)
    if existing and existing["checksum"] == checksum and existing["status"] == "ready" and not force:
        return f"không đổi, bỏ qua ({existing['num_chunks']} đoạn)"

    chunks = build_chunks(read_pages(path))
    if not chunks:
        return "không trích được chữ nào (có thể là PDF dạng ảnh scan), bỏ qua"

    # Gọi Gemini trước khi ghi gì vào CSDL: nếu lỗi thì dữ liệu cũ vẫn còn nguyên
    vectors = embed([c["content"] for c in chunks], "RETRIEVAL_DOCUMENT")

    key = store.storage_key(name, checksum)
    doc = store.save_document(name, checksum, key, existing)
    try:
        store.upload_file(key, data, CONTENT_TYPES[path.suffix.lower()])
        store.replace_chunks(doc["id"], [{**c, "embedding": v} for c, v in zip(chunks, vectors)])
        store.mark_ready(doc["id"], len(chunks))
    except Exception as e:
        store.mark_error(doc["id"], str(e))
        raise
    if existing and existing.get("storage_path") and existing["storage_path"] != key:
        store.remove_files([existing["storage_path"]])

    n_dieu = len({c["dieu"] for c in chunks if c["dieu"] is not None})
    return f"{len(chunks)} đoạn" + (f", {n_dieu} Điều" if n_dieu else "")


def main():
    parser = argparse.ArgumentParser(description="Nạp tài liệu trong data/ vào Supabase")
    parser.add_argument("--force", action="store_true", help="nạp lại cả những file không thay đổi")
    parser.add_argument("--prune", action="store_true", help="xoá các tài liệu không còn trong data/")
    args = parser.parse_args()

    files = sorted(p for p in DATA_DIR.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
    if not files:
        print(f"Không có file nào ({', '.join(sorted(SUPPORTED))}) trong {DATA_DIR}")

    failed = 0
    for path in files:
        print(f"- {path.name}: ", end="", flush=True)
        try:
            print(ingest_file(path, force=args.force))
        except Exception as e:
            failed += 1
            print(f"LỖI: {e}")

    if args.prune:
        names = {p.relative_to(DATA_DIR).as_posix() for p in files}
        for doc in store.list_documents():
            if doc["name"] not in names:
                store.delete_document(store.find_document(doc["name"]))
                print(f"- đã xoá {doc['name']} (không còn trong data/)")

    docs = store.list_documents()
    total = sum(d["num_chunks"] for d in docs)
    print(f"\nSupabase hiện có {len(docs)} tài liệu, {total} đoạn." + (f" {failed} file lỗi." if failed else ""))


if __name__ == "__main__":
    main()
