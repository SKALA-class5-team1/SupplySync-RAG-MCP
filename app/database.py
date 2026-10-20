import hashlib
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from app.config import Settings
from app.embeddings import Embedder, EmbeddingError
from app.schemas import Chunk, DocType, SearchHit, UploadResult


@contextmanager
def connect_db(settings: Settings):
    # 5432의 Supabase session pooler를 사용합니다.
    with psycopg.connect(
        host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
        user=settings.db_user, password=settings.db_password.get_secret_value(),
        sslmode=settings.db_sslmode,
        connect_timeout=10, autocommit=True, row_factory=dict_row,
    ) as conn:
        conn.execute("SELECT set_config('search_path', %s, false)", (settings.db_schema,))
        register_vector(conn)
        yield conn


def search_chunks(
    settings: Settings, embedder: Embedder, query: str, doc_type: DocType, k: int,
) -> list[SearchHit]:
    query_vector = embedder.encode([query])[0]
    with connect_db(settings) as conn:
        with conn.transaction():
            # 문서 유형 필터 때문에 HNSW 후보가 부족하면 추가 탐색합니다(pgvector 0.8+).
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            rows = conn.execute(
                """SELECT chunk_id, section, content,
                          1 - (embedding <=> %s) AS score
                     FROM public.doc_chunks
                    WHERE doc_type = %s
                      AND embedding IS NOT NULL
                      AND embedding_status = 'DONE'
                      AND embedding_model = %s
                    ORDER BY embedding <=> %s
                    LIMIT %s""",
                (query_vector, doc_type, settings.embedding_model, query_vector, k),
            ).fetchall()
    return [SearchHit.model_validate(row) for row in rows]


def save_chunks(
    settings: Settings, embedder: Embedder,
    doc_type: DocType, source: str, chunks: list[Chunk],
) -> UploadResult:
    # DB_USER를 사용합니다. RAG_DB_USER와 POSTGRES_ADMIN_USER는 사용하지 않습니다.
    with connect_db(settings) as conn:
        # 같은 문서의 동시 업로드만 직렬화합니다. 연결 종료 시 잠금이 해제됩니다.
        key = int.from_bytes(hashlib.sha256(f"{doc_type}\0{source}".encode()).digest()[:8], signed=True)
        conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        existing = conn.execute(
            """SELECT chunk_id, section, content_hash, embedding_status, embedding_model,
                      embedding IS NOT NULL AS has_embedding
                 FROM public.doc_chunks WHERE doc_type = %s AND source = %s""",
            (doc_type, source),
        ).fetchall()
        by_section = {row["section"]: row for row in existing}
        if len(by_section) != len(existing):
            raise ValueError("기존 문서에 중복 섹션이 있습니다. source와 section을 확인하세요.")

        changed = []
        for chunk in chunks:
            old = by_section.get(chunk.section)
            if old:
                # 제공된 시드의 dsn-001 같은 기존 ID도 유지합니다.
                chunk.chunk_id = old["chunk_id"]
            if not old or not (
                old["content_hash"] == chunk.content_hash
                and old["embedding_status"] == "DONE"
                and old["embedding_model"] == settings.embedding_model
                and old["has_embedding"]
            ):
                changed.append(chunk)

        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO public.doc_chunks
                           (chunk_id, doc_type, source, section, content, content_hash, embedding_status)
                       VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                       ON CONFLICT (chunk_id) DO UPDATE SET
                           section = EXCLUDED.section, content = EXCLUDED.content,
                           content_hash = EXCLUDED.content_hash,
                           embedding = NULL, embedding_status = 'PENDING',
                           embedding_model = NULL, embedded_at = NULL""",
                    [(c.chunk_id, doc_type, source, c.section, c.content, c.content_hash) for c in changed],
                )
            # 문서 재업로드 시 사라진 섹션은 함께 제거합니다.
            conn.execute(
                """DELETE FROM public.doc_chunks
                    WHERE doc_type = %s AND source = %s AND NOT (chunk_id = ANY(%s))""",
                (doc_type, source, [c.chunk_id for c in chunks]),
            )

        if changed:
            try:
                vectors = embedder.encode([c.content for c in changed])
            except EmbeddingError:
                conn.execute(
                    """UPDATE public.doc_chunks SET embedding_status = 'FAILED'
                        WHERE chunk_id = ANY(%s)""",
                    ([c.chunk_id for c in changed],),
                )
                raise
            with conn.transaction():
                with conn.cursor() as cursor:
                    cursor.executemany(
                        """UPDATE public.doc_chunks
                              SET embedding = %s, embedding_status = 'DONE',
                                  embedding_model = %s, embedded_at = CURRENT_TIMESTAMP
                            WHERE chunk_id = %s""",
                        [(vector, settings.embedding_model, c.chunk_id)
                         for c, vector in zip(changed, vectors, strict=True)],
                    )

    return UploadResult(
        doc_type=doc_type, source=source, chunk_count=len(chunks),
        embedded_chunks=len(changed), unchanged_chunks=len(chunks) - len(changed),
        embedding_model=settings.embedding_model,
    )
