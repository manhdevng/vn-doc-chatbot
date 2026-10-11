from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()                 # đọc GEMINI_API_KEY từ file .env
client = genai.Client()       # tự lấy key từ biến môi trường

# 1) Thử LLM
resp = client.models.generate_content(
    model="gemini-3.5-flash",
    contents="Giới thiệu RAG trong 1 câu bằng tiếng Việt.",
)
print("LLM:", resp.text)

# 2) Thử embedding
emb = client.models.embed_content(
    model="gemini-embedding-001",
    contents="Xin chào Việt Nam",
    config=types.EmbedContentConfig(
        task_type="RETRIEVAL_DOCUMENT",
        output_dimensionality=768,
    ),
)
print("Số chiều vector:", len(emb.embeddings[0].values))
