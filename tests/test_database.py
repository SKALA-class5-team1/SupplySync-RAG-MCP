"""RUN_DB_TESTS=1로 실행. 실제 pgvector와 테스트용 1024차원 벡터를 사용합니다."""
import hashlib
import os
from uuid import uuid4

import numpy as np
import pytest

from app.config import Settings
from app.database import connect_db, save_chunks
from app.embeddings import EmbeddingError
from app.schemas import Chunk

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="실제 PostgreSQL/pgvector가 필요합니다: RUN_DB_TESTS=1",
)


class TestEmbedder:
    __test__ = False

    def __init__(self):
        self.calls = 0
        self.fail = False

    def encode(self, texts):
        self.calls += len(texts)
        if self.fail:
            raise EmbeddingError("test failure")
        vectors = np.zeros((len(texts), 1024), dtype=np.float32)
        vectors[:, 0] = 1.0
        return vectors


@pytest.fixture
def db():
    settings = Settings()
    source = "test-" + uuid4().hex[:16] + ".md"
    with connect_db(settings) as conn:
        try:
            yield settings, source, conn
        finally:
            # 이 테스트가 생성한 고유 source의 행만 정리합니다.
            conn.execute("DELETE FROM public.doc_chunks WHERE source = %s", (source,))


def chunk(source, section, content):
    identity = hashlib.sha256(f"{source}:{section}".encode()).hexdigest()[:24]
    return Chunk("tst-" + identity, section, content, hashlib.sha256(content.encode()).hexdigest())


def test_store_reuse_update_and_remove_stale_chunks(db):
    settings, source, conn = db
    embedder = TestEmbedder()
    chunks = [chunk(source, "1. Model", "EWP-12-B"), chunk(source, "2. Temperature", "125 C")]

    result = save_chunks(settings, embedder, "catalog", source, chunks)
    assert result.embedded_chunks == 2 and embedder.calls == 2
    rows = conn.execute(
        "SELECT *, vector_dims(embedding) AS dims FROM public.doc_chunks WHERE source = %s ORDER BY section",
        (source,),
    ).fetchall()
    assert all(row["dims"] == 1024 and row["embedding_status"] == "DONE" for row in rows)
    assert all(row["embedding_model"] == "BAAI/bge-m3" and row["embedded_at"] for row in rows)
    assert all(abs(np.linalg.norm(row["embedding"]) - 1) < 1e-6 for row in rows)

    again = save_chunks(settings, embedder, "catalog", source, chunks)
    assert again.unchanged_chunks == 2 and embedder.calls == 2
    assert save_chunks(settings, embedder, "catalog", source, [chunk(source, "1. Model", "EWP-12-C")]).embedded_chunks == 1
    rows = conn.execute("SELECT content FROM public.doc_chunks WHERE source = %s", (source,)).fetchall()
    assert rows == [{"content": "EWP-12-C"}]


def test_embedding_failure_clears_old_vector_and_retry_recovers(db):
    settings, source, conn = db
    embedder = TestEmbedder()
    save_chunks(settings, embedder, "policy", source, [chunk(source, "제10조", "old")])
    embedder.fail = True
    with pytest.raises(EmbeddingError):
        save_chunks(settings, embedder, "policy", source, [chunk(source, "제10조", "new")])
    row = conn.execute(
        "SELECT embedding, embedding_status, embedded_at FROM public.doc_chunks WHERE source = %s", (source,),
    ).fetchone()
    assert row == {"embedding": None, "embedding_status": "FAILED", "embedded_at": None}
    embedder.fail = False
    result = save_chunks(settings, embedder, "policy", source, [chunk(source, "제10조", "new")])
    assert result.embedded_chunks == 1


def test_existing_seed_id_is_preserved(db):
    settings, source, conn = db
    seed_id = "seed-" + uuid4().hex[:20]
    conn.execute(
        """INSERT INTO public.doc_chunks (chunk_id, doc_type, source, section, content, content_hash)
           VALUES (%s, 'design_std', %s, '1. Test', 'old', %s)""",
        (seed_id, source, hashlib.sha256(b"old").hexdigest()),
    )
    save_chunks(settings, TestEmbedder(), "design_std", source, [chunk(source, "1. Test", "new")])
    rows = conn.execute("SELECT chunk_id FROM public.doc_chunks WHERE source = %s", (source,)).fetchall()
    assert rows == [{"chunk_id": seed_id}]
