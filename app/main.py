import hmac
import json
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader
from pydantic import ValidationError

# main.py를 직접 실행해도 프로젝트의 app 패키지를 찾을 수 있게 합니다.
if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.database import save_chunks
from app.embeddings import Embedder, EmbeddingError
from app.ingest import prepare_chunks
from app.mcp_server import MCPApiKeyMiddleware, create_mcp
from app.schemas import UploadMetadata, UploadResult

internal_key = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    embedder = Embedder(settings)
    mcp = create_mcp(settings, embedder)
    # session_manager를 시작하기 전에 MCP의 ASGI 앱을 생성합니다.
    mcp_app = mcp.streamable_http_app()
    mcp_app.add_middleware(
        MCPApiKeyMiddleware, api_key=settings.internal_api_key.get_secret_value(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="SupplySync RAG MCP", lifespan=lifespan)

    def require_internal_key(key: str | None = Security(internal_key)) -> None:
        if not key or not hmac.compare_digest(
            key.encode(), settings.internal_api_key.get_secret_value().encode(),
        ):
            raise HTTPException(status_code=401, detail="내부 API 키가 올바르지 않습니다.")

    @app.get("/health")
    def health() -> dict[str, str]:
        # 프로세스 상태입니다. DB 연결이나 모델 다운로드를 시작하지 않습니다.
        return {"status": "ok"}

    @app.post("/internal/documents", dependencies=[Depends(require_internal_key)])
    def upload_document(
        file: Annotated[UploadFile, File(description="UTF-8 Markdown 문서(.md 또는 .markdown)")],
        metadata: Annotated[str, Form(description=(
            'JSON: {"doc_type":"design_std","source":"design_standard.md",'
            '"additional_sections":[]}'
        ), max_length=500000)],
    ) -> UploadResult:
        try:
            info = UploadMetadata.model_validate_json(metadata)
            data = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
            if len(data) > settings.max_upload_mb * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"문서는 {settings.max_upload_mb}MB 이하여야 합니다.")
            source, chunks = prepare_chunks(data, file.filename or "", info)
            return save_chunks(settings, embedder, info.doc_type, source, chunks)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=json.loads(exc.json(include_input=False, include_url=False)),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except EmbeddingError as exc:
            raise HTTPException(
                status_code=503,
                detail="임베딩 생성에 실패해 해당 청크를 FAILED로 기록했습니다. 모델 설정을 확인하고 같은 문서를 재업로드하세요.",
            ) from exc
        except psycopg.Error as exc:
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL 연결, pgvector 설치, doc_chunks 테이블과 쓰기 계정 권한을 확인하세요.",
            ) from exc
        finally:
            file.file.close()

    # SDK 앱의 내부 경로가 /mcp입니다. REST 경로 뒤에 붙여 같은 포트에서 제공합니다.
    app.mount("/", mcp_app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8102)
