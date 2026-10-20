-- 기존 테이블이 있으면 새 테이블을 만들지 않습니다.
-- vector 확장이 없다면 DBA가 먼저 설치/활성화해야 합니다.
SET search_path = public, extensions;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.doc_chunks (
    chunk_id         VARCHAR(30) PRIMARY KEY,
    doc_type         VARCHAR(20) NOT NULL
                     CHECK (doc_type IN ('design_std', 'catalog', 'policy', 'alt_parts')),
    source           VARCHAR(50) NOT NULL,
    section          VARCHAR(50) NOT NULL,
    content          TEXT NOT NULL,
    embedding        vector(1024),
    embedding_status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                     CHECK (embedding_status IN ('PENDING', 'DONE', 'FAILED')),
    embedding_model  VARCHAR(50),
    embedded_at      TIMESTAMP,
    content_hash     VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_type ON public.doc_chunks (doc_type);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
    ON public.doc_chunks USING hnsw (embedding vector_cosine_ops);

