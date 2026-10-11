"""Đọc/ghi Supabase: bảng documents, chunks, Storage và hàm tìm kiếm match_chunks.

Lược đồ CSDL nằm trong supabase/migrations/.
"""

import re
import unicodedata

try:
    from app.core import BUCKET, supabase
except ImportError:  # chạy trực tiếp một script trong app/
    from core import BUCKET, supabase

INSERT_BATCH = 100   # số dòng chunks ghi trong một request


# ---------- Tài liệu ----------

def find_document(name: str) -> dict | None:
    rows = supabase().table("documents").select("*").eq("name", name).limit(1).execute().data
    return rows[0] if rows else None


def list_documents() -> list[dict]:
    return (
        supabase().table("documents")
        .select("id, name, status, num_chunks, updated_at")
        .order("name")
        .execute().data
    )


def save_document(name: str, checksum: str, storage_path: str, existing: dict | None) -> dict:
    """Tạo mới hoặc cập nhật dòng documents, đặt trạng thái 'processing'."""
    row = {"name": name, "checksum": checksum, "storage_path": storage_path, "status": "processing", "error": None}
    table = supabase().table("documents")
    if existing:
        return table.update(row).eq("id", existing["id"]).execute().data[0]
    return table.insert(row).execute().data[0]


def mark_ready(document_id: str, num_chunks: int) -> None:
    supabase().table("documents").update({"status": "ready", "num_chunks": num_chunks}).eq("id", document_id).execute()


def mark_error(document_id: str, message: str) -> None:
    supabase().table("documents").update({"status": "error", "error": message[:1000]}).eq("id", document_id).execute()


def delete_document(doc: dict) -> None:
    """Xoá tài liệu, các đoạn của nó (cascade) và file gốc trong Storage."""
    if doc.get("storage_path"):
        remove_files([doc["storage_path"]])
    supabase().table("documents").delete().eq("id", doc["id"]).execute()


# ---------- Storage ----------

def storage_key(name: str, checksum: str) -> str:
    """Storage chỉ nhận tên ASCII an toàn: 'Luật Viên chức.pdf' → '<checksum>/Luat_Vien_chuc.pdf'."""
    ascii_name = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    ascii_name = "".join(c for c in ascii_name if not unicodedata.combining(c))
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("_") or "file"
    return f"{checksum[:16]}/{ascii_name}"


def upload_file(key: str, data: bytes, content_type: str) -> None:
    supabase().storage.from_(BUCKET).upload(key, data, {"content-type": content_type, "upsert": "true"})


def remove_files(keys: list[str]) -> None:
    if keys:
        supabase().storage.from_(BUCKET).remove(keys)


# ---------- Đoạn tài liệu ----------

def replace_chunks(document_id: str, rows: list[dict]) -> None:
    """Xoá toàn bộ đoạn cũ của tài liệu rồi ghi các đoạn mới.

    Mỗi dòng: {chunk_index, chuong, dieu, page, content, embedding}.
    """
    sb = supabase()
    sb.table("chunks").delete().eq("document_id", document_id).execute()
    for i in range(0, len(rows), INSERT_BATCH):
        batch = [{**r, "document_id": document_id} for r in rows[i : i + INSERT_BATCH]]
        sb.table("chunks").insert(batch).execute()


def match_chunks(embedding: list[float], query_text: str, k: int = 5, document_id: str | None = None) -> list[dict]:
    """Tìm kiếm kết hợp vector + từ khoá. Trả về các dòng đã xếp hạng theo điểm RRF."""
    params = {"query_embedding": embedding, "query_text": query_text, "match_count": k}
    if document_id:
        params["filter_document_id"] = document_id
    return supabase().rpc("match_chunks", params).execute().data


def get_article(dieu: int, document_id: str | None = None) -> list[dict]:
    """Lấy nguyên văn một Điều (có thể gồm nhiều đoạn), theo thứ tự. Không truyền document_id thì tìm trong mọi tài liệu."""
    query = (
        supabase().table("chunks")
        .select("id, chunk_index, chuong, dieu, page, content, document_id, documents(name)")
        .eq("dieu", dieu)
    )
    if document_id:
        query = query.eq("document_id", document_id)
    rows = query.order("document_id").order("chunk_index").execute().data
    return [{**r, "document_name": r.pop("documents")["name"]} for r in rows]
