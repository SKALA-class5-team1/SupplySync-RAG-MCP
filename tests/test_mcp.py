import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.embeddings import Embedder, EmbeddingError
from app.main import create_app
from app.schemas import SearchHit

HEADERS = {
    "X-Internal-Api-Key": "test-key",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def client(monkeypatch):
    def unexpected_work(*args, **kwargs):
        pytest.fail("MCP 초기화에서 DB 접근이나 임베딩을 실행하면 안 됩니다.")

    monkeypatch.setattr("app.main.save_chunks", unexpected_work)
    monkeypatch.setattr("app.main.Embedder.encode", unexpected_work)
    monkeypatch.setattr("app.mcp_server.search_chunks", unexpected_work)
    settings = Settings(
        _env_file=None, db_password="unused", rag_db_password="unused",
        rabbitmq_password="unused", internal_api_key="test-key",
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8102") as client:
        yield client


def test_real_mcp_initialize_ping_and_search_tool_schema(client):
    headers = HEADERS.copy()
    initialized = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    })
    assert initialized.status_code == 200
    result = initialized.json()["result"]
    assert result["serverInfo"]["name"] == "SupplySync RAG MCP"
    headers["MCP-Protocol-Version"] = result["protocolVersion"]
    notified = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert notified.status_code == 202
    listed = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    })
    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["rag_search_docs"]
    tool = tools[0]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["inputSchema"]["required"] == ["query", "doc_type"]
    fields = tool["inputSchema"]["properties"]
    assert fields["doc_type"]["enum"] == ["design_std", "catalog", "policy", "alt_parts"]
    assert fields["k"]["default"] == 4
    assert fields["k"]["minimum"] == 1 and fields["k"]["maximum"] == 20
    assert tool["outputSchema"]["properties"]["result"]["type"] == "array"
    ping = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "ping",
    })
    assert ping.json() == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("key", ["", "wrong-key"])
def test_mcp_requires_internal_key_on_every_method(client, method, key):
    response = client.request(method, "/mcp", headers={"X-Internal-Api-Key": key})
    assert response.status_code == 401


def call_search(client, arguments):
    response = client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "rag_search_docs", "arguments": arguments},
    })
    assert response.status_code == 200
    return response.json()["result"]


@pytest.mark.parametrize("has_results", [True, False])
def test_search_returns_structured_results_and_default_k(client, monkeypatch, has_results):
    hits = [SearchHit(
        chunk_id="pol-003", section="제12조 품질 보증 기간",
        content="품질 보증 기간은 양산 시작일부터 3년이다.", score=0.82,
    )] if has_results else []
    calls = []

    def search(settings, embedder, query, doc_type, k):
        assert settings.internal_api_key.get_secret_value() == "test-key"
        assert isinstance(embedder, Embedder)
        calls.append((query, doc_type, k, embedder))
        return hits

    monkeypatch.setattr("app.mcp_server.search_chunks", search)
    result = call_search(client, {"query": "  고객 요구 보증 5년 15만km  ", "doc_type": "policy"})
    assert result["isError"] is False
    assert result["content"] == []
    assert result["structuredContent"]["result"] == [hit.model_dump() for hit in hits]
    assert calls[0][:3] == ("고객 요구 보증 5년 15만km", "policy", 4)
    call_search(client, {"query": "작동 온도", "doc_type": "design_std", "k": 2})
    assert calls[1][:3] == ("작동 온도", "design_std", 2)
    assert calls[0][3] is calls[1][3]


@pytest.mark.parametrize("arguments", [
    {"query": " ", "doc_type": "policy"},
    {"query": "a" * 2001, "doc_type": "policy"},
    {"query": "보증", "doc_type": "unknown"},
    {"query": "보증"},
    {"query": "보증", "doc_type": "policy", "k": 0},
    {"query": "보증", "doc_type": "policy", "k": 21},
    {"query": "보증", "doc_type": "policy", "k": 1.5},
])
def test_invalid_search_arguments_are_rejected_before_db_or_model(client, arguments):
    result = call_search(client, arguments)
    assert result["isError"] is True


@pytest.mark.parametrize("error", [
    psycopg.OperationalError("password=private-password host=private-host"),
    EmbeddingError("private-model-path"),
])
def test_search_errors_do_not_leak_details(client, monkeypatch, error):
    def fail(*args):
        raise error

    monkeypatch.setattr("app.mcp_server.search_chunks", fail)
    result = call_search(client, {"query": "보증", "doc_type": "policy", "k": 4})
    assert result["isError"] is True
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"]
    assert "private" not in str(result)
