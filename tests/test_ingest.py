import hashlib
import json
from types import SimpleNamespace

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.embeddings import Embedder, EmbeddingError
from app.ingest import prepare_chunks, split_sections
from app.main import create_app
from app.schemas import UploadMetadata, UploadResult


@pytest.fixture
def settings():
    return Settings(
        _env_file=None, db_password="unused", rag_db_password="unused",
        rabbitmq_password="unused", internal_api_key="test-key", max_upload_mb=1,
    )


def test_markdown_sections_exclude_document_title_and_preamble():
    data = "# 설계 표준\n\n문서 소개\n\n## 1. 작동 온도\n상한은 125℃다.\n\n## 2. 유량\n25 L/min.\n".encode()
    source, chunks = prepare_chunks(data, "standard.md", UploadMetadata(doc_type="design_std"))
    assert source == "standard.md"
    assert [c.section for c in chunks] == ["1. 작동 온도", "2. 유량"]
    assert chunks[0].content == "상한은 125℃다."
    assert chunks[1].content == "25 L/min."
    assert all(len(c.chunk_id) <= 30 and len(c.content_hash) == 64 for c in chunks)


def test_korean_numbered_and_policy_headings():
    sections = split_sections(
        "# 설계 표준\n## 1. 작동 온도\n고온 옵션은 125℃까지 지원한다.\n## 제10조 견적 검토 원칙\n검토 후 제출한다.",
    )
    assert [s.section for s in sections] == ["1. 작동 온도", "제10조 견적 검토 원칙"]
    assert sections[0].content == "고온 옵션은 125℃까지 지원한다."


def test_markdown_without_section_headings_keeps_body():
    _, chunks = prepare_chunks(b"# Catalog\n\nPlain document.", "plain.md", UploadMetadata(doc_type="catalog"))
    assert chunks[0].section == "본문"
    assert chunks[0].content == "Plain document."


def test_supplement_is_included_in_hash():
    metadata = UploadMetadata.model_validate({
        "doc_type": "design_std",
        "source": "design_standard.md",
        "additional_sections": [
            {"section": "1. 작동 온도", "content": "검증 기간은 2주다."},
            {"section": "고객 확인 사항", "content": "냉각수 종류를 확인해야 한다."},
        ],
    })
    source, chunks = prepare_chunks("## 1. 작동 온도\n상한은 125℃다.".encode(), "upload.md", metadata)
    assert source == "design_standard.md"
    assert len(chunks) == 2
    assert chunks[0].content == "상한은 125℃다.\n검증 기간은 2주다."
    assert chunks[0].content_hash == hashlib.sha256(chunks[0].content.encode()).hexdigest()


def test_reupload_ids_are_stable_and_different_sources_are_isolated():
    metadata = UploadMetadata(doc_type="catalog")
    _, first = prepare_chunks(b"## 1. Model\nold", "one.md", metadata)
    _, changed = prepare_chunks(b"## 1. Model\nnew", "one.md", metadata)
    _, other = prepare_chunks(b"## 1. Model\nnew", "two.md", metadata)
    assert first[0].chunk_id == changed[0].chunk_id
    assert first[0].content_hash != changed[0].content_hash
    assert changed[0].chunk_id != other[0].chunk_id


def test_long_section_does_not_lose_tail():
    text = "a" * 3500 + "END"
    metadata = UploadMetadata(doc_type="catalog")
    _, chunks = prepare_chunks(("## 1. Long\n" + text).encode(), "long.md", metadata)
    assert [c.section for c in chunks] == ["1. Long", "1. Long (2)", "1. Long (3)"]
    assert all(len(c.content) <= 1600 for c in chunks)
    assert chunks[0].content + chunks[1].content[150:] + chunks[2].content[150:] == text


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_markdown_lists_tables_and_code_blocks_are_preserved(fence):
    body = (
        "1. 확인 항목\n2. 다음 항목\n\n| 모델 | 온도 |\n| --- | --- |\n| A | 125℃ |\n\n"
        f"{fence}md\n## 코드 안의 제목\n{fence}"
    )
    sections = split_sections(f"## 사양 ###\n{body}\n\n### 보증\n2년")
    assert [s.section for s in sections] == ["사양", "보증"]
    assert sections[0].content == body


def test_utf8_bom_crlf_and_filename_path():
    data = "\ufeff# 문서\r\n\r\n## 작동 온도\r\n125℃\r\n".encode()
    source, chunks = prepare_chunks(data, r"C:\docs\standard.MD", UploadMetadata(doc_type="design_std"))
    assert source == "standard.MD"
    assert chunks[0].section == "작동 온도" and chunks[0].content == "125℃"


@pytest.mark.parametrize("filename,data,message", [
    ("doc.pdf", b"%PDF-1.7", "Markdown 파일"),
    ("doc.md", b"\xff\xfe", "UTF-8"),
    ("doc.md", b"text\x00", "NUL"),
    ("empty.md", b" \n ", "저장할 본문"),
    ("empty.md", b"# Title\n\n## Empty section\n", "저장할 본문"),
])
def test_invalid_files_are_422_before_storage(settings, monkeypatch, filename, data, message):
    def unexpected_save(*args):
        pytest.fail("유효하지 않은 문서는 저장하면 안 됩니다.")

    monkeypatch.setattr("app.main.save_chunks", unexpected_save)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/internal/documents", headers={"X-Internal-Api-Key": "test-key"},
            data={"metadata": '{"doc_type":"catalog","source":"override.md"}'},
            files={"file": (filename, data, "application/octet-stream")},
        )
        assert response.status_code == 422
        assert message in response.json()["detail"]


def test_upload_auth_metadata_and_supplement(settings, monkeypatch):
    received = []

    def save(settings, embedder, doc_type, source, chunks):
        received.extend(chunks)
        return UploadResult(
            doc_type=doc_type, source=source, chunk_count=len(chunks),
            embedded_chunks=len(chunks), unchanged_chunks=0,
            embedding_model="BAAI/bge-m3",
        )

    monkeypatch.setattr("app.main.save_chunks", save)
    with TestClient(create_app(settings)) as client:
        files = {"file": ("catalog.md", b"## 1. Model\nEWP-12-B", "text/markdown")}
        data = {"metadata": json.dumps({
            "doc_type": "catalog",
            "additional_sections": [{"section": "1. Model", "content": "추가 확인값"}],
        })}
        assert client.post("/internal/documents", data=data, files=files).status_code == 401
        assert client.post("/internal/documents", data=data, files=files,
                           headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
        assert received == []
        response = client.post("/internal/documents", data=data, files=files,
                               headers={"X-Internal-Api-Key": "test-key"})
        assert response.status_code == 200
        assert response.json()["source"] == "catalog.md"
        assert response.json()["embedding_status"] == "DONE"
        assert received[0].content == "EWP-12-B\n추가 확인값"
        assert set(client.get("/openapi.json").json()["paths"]) == {"/health", "/internal/documents"}


@pytest.mark.parametrize("metadata", [
    "not json", '{"doc_type":"wrong"}', '{"doc_type":"catalog","source":""}',
    '{"doc_type":"catalog","page_texts":{"1":"text"}}',
])
def test_invalid_metadata_is_422(settings, metadata):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/internal/documents", headers={"X-Internal-Api-Key": "test-key"},
            data={"metadata": metadata},
            files={"file": ("doc.md", b"Text", "text/markdown")},
        )
        assert response.status_code == 422


def test_file_size_limit(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/internal/documents", headers={"X-Internal-Api-Key": "test-key"},
            data={"metadata": '{"doc_type":"catalog"}'},
            files={"file": ("big.md", b"x" * (1024 * 1024 + 1), "text/markdown")},
        )
        assert response.status_code == 413


@pytest.mark.parametrize("error", [psycopg.OperationalError("password=secret"), EmbeddingError("private details")])
def test_storage_errors_do_not_leak_details(settings, monkeypatch, error):
    def fail(*args):
        raise error

    monkeypatch.setattr("app.main.save_chunks", fail)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/internal/documents", headers={"X-Internal-Api-Key": "test-key"},
            data={"metadata": '{"doc_type":"catalog"}'},
            files={"file": ("doc.md", b"Text", "text/markdown")},
        )
        assert response.status_code == 503
        assert "secret" not in response.text and "private details" not in response.text


def test_embedding_normalizes_1024_dimensions(settings):
    embedder = Embedder(settings)
    embedder._model = SimpleNamespace(encode=lambda texts, **kwargs: np.full((len(texts), 1024), 2.0))
    vectors = embedder.encode(["one", "two"])
    assert vectors.shape == (2, 1024)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0)


@pytest.mark.parametrize("values", [
    np.zeros((1, 1024)), np.ones((1, 768)), np.full((1, 1024), np.nan), np.full((1, 1024), np.inf),
])
def test_invalid_embeddings_cannot_be_saved(settings, values):
    embedder = Embedder(settings)
    embedder._model = SimpleNamespace(encode=lambda *args, **kwargs: values)
    with pytest.raises(EmbeddingError):
        embedder.encode(["text"])
