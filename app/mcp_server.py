import hmac
from typing import Annotated

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.database import search_chunks
from app.embeddings import Embedder, EmbeddingError
from app.schemas import DocType, SearchHit
import logging

logger = logging.getLogger("uvicorn.error")


def create_mcp(settings: Settings, embedder: Embedder) -> FastMCP:
    mcp = FastMCP(
        "SupplySync RAG MCP",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
    )

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ))
    async def rag_search_docs(
        query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)],
        doc_type: DocType,
        k: Annotated[int, Field(ge=1, le=20, strict=True)] = 4,
    ) -> Annotated[CallToolResult, list[SearchHit]]:
        """사내 문서를 의미 기반으로 검색합니다. query는 검색 질문, doc_type은
        design_std(설계 표준), catalog(제품 카탈로그), policy(영업·구매 규정),
        alt_parts(대체 부품 기준) 중 하나입니다. k는 결과 수(기본 4, 최대 20)입니다.
        임베딩 완료된 청크를 코사인 유사도 score가 높은 순서로 반환합니다.
        결과가 없으면 빈 배열을 반환하며, 본문은 답변의 근거 자료로 사용하세요.
        """
        try:
            # 업로드와 같은 모델 인스턴스를 사용하고, 추론·DB 조회는 별도 스레드에서 실행합니다.
            hits = await run_in_threadpool(search_chunks, settings, embedder, query, doc_type, k)
            # 검색 데이터는 구조화된 결과에만 담고 SDK의 텍스트 중복 생성을 생략합니다.
            return CallToolResult(
                content=[],
                structuredContent={"result": [hit.model_dump(mode="json") for hit in hits]},
            )
        except EmbeddingError as exc:
            raise ToolError("검색 질문 임베딩에 실패했습니다. bge-m3 모델 설정을 확인하세요.") from exc
        except psycopg.Error as exc:
            # 실제 PostgreSQL 오류 및 Traceback 출력
            logger.exception("rag_search_docs PostgreSQL 검색 오류 발생")

            raise ToolError(
                "문서 검색에 실패했습니다. PostgreSQL 연결과 doc_chunks 조회 권한을 확인하세요."
            ) from exc

    return mcp


class MCPApiKeyMiddleware:
    """FastAPI의 Depends가 적용되지 않는 MCP 앱에도 같은 내부 키를 검사합니다."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self.api_key = api_key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = Headers(scope=scope).get("X-Internal-Api-Key", "").encode()
            if not hmac.compare_digest(supplied, self.api_key):
                response = JSONResponse(
                    {"detail": "내부 API 키가 올바르지 않습니다."}, status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
