"""Cấu hình, client và hàm embedding dùng chung cho ingest và rag."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import Client, create_client
from tenacity import retry, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")

LLM_MODEL = "gemini-3.5-flash"
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768          # phải khớp với cột chunks.embedding vector(768) trong Supabase
EMBED_BATCH = 100        # số đoạn gửi trong một lần gọi API embedding

BUCKET = "documents"     # bucket Supabase Storage chứa file gốc


@lru_cache
def gemini() -> genai.Client:
    return genai.Client()


@lru_cache
def supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise RuntimeError("Thiếu SUPABASE_URL hoặc SUPABASE_SECRET_KEY trong .env")
    return create_client(url, key)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=60))
def _embed_batch(texts: list[str], task_type: str) -> list[list[float]]:
    resp = gemini().models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=EMBED_DIM),
    )
    return [e.values for e in resp.embeddings]


def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """task_type: "RETRIEVAL_DOCUMENT" khi nạp tài liệu, "RETRIEVAL_QUERY" khi embedding câu hỏi."""
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(_embed_batch(texts[i : i + EMBED_BATCH], task_type))
    return vectors
