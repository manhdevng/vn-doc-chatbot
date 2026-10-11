-- =====================================================================
-- vn-doc-chatbot: khởi tạo CSDL
-- Chạy một lần trong Supabase Dashboard → SQL Editor (chạy cả file).
-- File chạy lại nhiều lần vẫn an toàn (dùng IF NOT EXISTS / OR REPLACE).
-- =====================================================================

-- ---------- 1. Extension ----------
create extension if not exists vector with schema extensions;    -- lưu và tìm vector
create extension if not exists unaccent with schema extensions;  -- bỏ dấu tiếng Việt khi tìm từ khoá


-- ---------- 2. Hàm tiện ích ----------

-- Chuẩn hoá văn bản để tìm từ khoá: Unicode NFC → chữ thường → bỏ dấu ("Điều" → "dieu").
-- unaccent() gốc không phải IMMUTABLE nên cần bọc lại mới dùng được trong cột generated.
create or replace function public.vn_normalize(input text)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
  select extensions.unaccent('extensions.unaccent'::regdictionary, lower(normalize(coalesce(input, ''), NFC)));
$$;

-- Tự cập nhật cột updated_at
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;


-- ---------- 3. Bảng tài liệu ----------
create table if not exists public.documents (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,                         -- tên file hiển thị, vd "luat.pdf"
  storage_path  text unique,                           -- đường dẫn trong bucket "documents"
  checksum      text,                                  -- md5 nội dung file, để bỏ qua file không đổi khi nạp lại
  status        text not null default 'pending'
                check (status in ('pending', 'processing', 'ready', 'error')),
  num_chunks    integer not null default 0,
  error         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

drop trigger if exists documents_updated_at on public.documents;
create trigger documents_updated_at
  before update on public.documents
  for each row execute function public.set_updated_at();


-- ---------- 4. Bảng đoạn tài liệu (thay ChromaDB) ----------
create table if not exists public.chunks (
  id           bigint generated always as identity primary key,
  document_id  uuid not null references public.documents (id) on delete cascade,
  chunk_index  integer not null,                       -- thứ tự đoạn trong tài liệu
  chuong       text,                                   -- vd "Chương II"
  dieu         integer,                                -- số Điều, vd 12 (null nếu không phải văn bản luật)
  page         integer,                                -- trang bắt đầu trong PDF
  content      text not null,
  embedding    extensions.vector(768) not null,        -- gemini-embedding-001, 768 chiều
  fts          tsvector generated always as (to_tsvector('simple', public.vn_normalize(content))) stored,
  created_at   timestamptz not null default now(),
  unique (document_id, chunk_index)
);

-- Tìm theo ngữ nghĩa (cosine)
create index if not exists chunks_embedding_idx
  on public.chunks using hnsw (embedding extensions.vector_cosine_ops);
-- Tìm theo từ khoá
create index if not exists chunks_fts_idx
  on public.chunks using gin (fts);
-- Lấy nguyên văn một Điều: get_article(document_id, dieu)
create index if not exists chunks_document_dieu_idx
  on public.chunks (document_id, dieu);


-- ---------- 5. Hội thoại ----------
create table if not exists public.conversations (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid references auth.users (id) on delete cascade,   -- null = khách chưa đăng nhập
  title       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists conversations_user_idx
  on public.conversations (user_id, updated_at desc);

drop trigger if exists conversations_updated_at on public.conversations;
create trigger conversations_updated_at
  before update on public.conversations
  for each row execute function public.set_updated_at();


create table if not exists public.messages (
  id                 bigint generated always as identity primary key,
  conversation_id    uuid not null references public.conversations (id) on delete cascade,
  role               text not null check (role in ('user', 'assistant', 'tool')),
  content            text not null default '',
  sources            jsonb,     -- các đoạn đã trích dẫn: [{document_id, name, page, dieu, score}]
  tool_calls         jsonb,     -- các lần agent gọi công cụ: [{name, args, result_summary}]
  prompt_tokens      integer,
  completion_tokens  integer,
  created_at         timestamptz not null default now()
);

create index if not exists messages_conversation_idx
  on public.messages (conversation_id, created_at);


create table if not exists public.feedback (
  message_id  bigint primary key references public.messages (id) on delete cascade,
  user_id     uuid references auth.users (id) on delete cascade,
  rating      smallint not null check (rating in (-1, 1)),   -- 👎 = -1, 👍 = 1
  comment     text,
  created_at  timestamptz not null default now()
);


-- ---------- 6. Tìm kiếm kết hợp: vector + từ khoá (Reciprocal Rank Fusion) ----------
-- Gọi từ Python: sb.rpc("match_chunks", {"query_embedding": vec, "query_text": q, "match_count": 5})
create or replace function public.match_chunks(
  query_embedding     extensions.vector(768),
  query_text          text,
  match_count         integer default 5,
  filter_document_id  uuid default null,
  full_text_weight    float default 1.0,
  semantic_weight     float default 1.0,
  rrf_k               integer default 50
)
returns table (
  id             bigint,
  document_id    uuid,
  document_name  text,
  chuong         text,
  dieu           integer,
  page           integer,
  content        text,
  similarity     float,     -- độ giống cosine (0..1), để hiển thị
  score          float      -- điểm RRF tổng hợp, dùng để xếp hạng
)
language sql
stable
security invoker
set search_path = ''
as $$
  with
  -- Câu hỏi → các từ đã bỏ dấu, nối bằng OR ("quyen | vien | chuc ...")
  q as (
    select nullif(
      array_to_string(
        regexp_split_to_array(
          trim(regexp_replace(public.vn_normalize(query_text), '[^[:alnum:][:space:]]+', ' ', 'g')),
          '\s+'
        ),
        ' | '
      ),
      ''
    ) as tsq
  ),
  semantic as (
    select c.id,
           row_number() over (order by c.embedding operator(extensions.<=>) query_embedding) as rank_ix
    from public.chunks c
    where filter_document_id is null or c.document_id = filter_document_id
    order by c.embedding operator(extensions.<=>) query_embedding
    limit least(match_count, 30) * 2
  ),
  full_text as (
    select c.id,
           row_number() over (order by ts_rank_cd(c.fts, to_tsquery('simple', q.tsq)) desc) as rank_ix
    from public.chunks c, q
    where q.tsq is not null
      and c.fts @@ to_tsquery('simple', q.tsq)
      and (filter_document_id is null or c.document_id = filter_document_id)
    order by ts_rank_cd(c.fts, to_tsquery('simple', q.tsq)) desc
    limit least(match_count, 30) * 2
  )
  select c.id,
         c.document_id,
         d.name,
         c.chuong,
         c.dieu,
         c.page,
         c.content,
         (1 - (c.embedding operator(extensions.<=>) query_embedding))::float as similarity,
         (coalesce(1.0 / (rrf_k + s.rank_ix), 0.0) * semantic_weight
          + coalesce(1.0 / (rrf_k + f.rank_ix), 0.0) * full_text_weight)::float as score
  from semantic s
  full outer join full_text f on f.id = s.id
  join public.chunks c on c.id = coalesce(s.id, f.id)
  join public.documents d on d.id = c.document_id
  order by score desc
  limit least(match_count, 30);
$$;


-- ---------- 7. Bảo mật (Row Level Security) ----------
-- Server dùng SUPABASE_SECRET_KEY (bỏ qua RLS) để ghi tài liệu và lưu hội thoại.
-- Các policy dưới đây chỉ áp dụng khi trình duyệt gọi trực tiếp bằng publishable/anon key.
alter table public.documents     enable row level security;
alter table public.chunks        enable row level security;
alter table public.conversations enable row level security;
alter table public.messages      enable row level security;
alter table public.feedback      enable row level security;

-- Tài liệu: người đã đăng nhập được đọc, không ai được ghi từ trình duyệt
drop policy if exists "documents_read" on public.documents;
create policy "documents_read" on public.documents
  for select to authenticated using (true);

drop policy if exists "chunks_read" on public.chunks;
create policy "chunks_read" on public.chunks
  for select to authenticated using (true);

-- Hội thoại: mỗi người chỉ thấy và sửa của chính mình
drop policy if exists "conversations_owner" on public.conversations;
create policy "conversations_owner" on public.conversations
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

drop policy if exists "messages_owner" on public.messages;
create policy "messages_owner" on public.messages
  for all to authenticated
  using (exists (
    select 1 from public.conversations c
    where c.id = conversation_id and c.user_id = (select auth.uid())
  ))
  with check (exists (
    select 1 from public.conversations c
    where c.id = conversation_id and c.user_id = (select auth.uid())
  ));

drop policy if exists "feedback_owner" on public.feedback;
create policy "feedback_owner" on public.feedback
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

-- Hàm tìm kiếm: không cho khách vô danh gọi
revoke execute on function public.match_chunks(extensions.vector, text, integer, uuid, float, float, integer) from public, anon;
grant  execute on function public.match_chunks(extensions.vector, text, integer, uuid, float, float, integer) to authenticated, service_role;


-- ---------- 8. Storage: bucket riêng tư chứa file gốc ----------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'documents', 'documents', false,
  52428800,                                            -- tối đa 50 MB / file
  array['application/pdf', 'text/plain', 'text/markdown']
)
on conflict (id) do nothing;


-- ---------- Kiểm tra sau khi chạy (tuỳ chọn) ----------
-- select public.vn_normalize('Điều 12. Quyền của Viên chức');   -- → "dieu 12. quyen cua vien chuc"
-- select table_name from information_schema.tables where table_schema = 'public' order by 1;
