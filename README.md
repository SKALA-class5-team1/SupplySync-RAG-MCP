# SupplySync RAG MCP

Markdown 문서를 받아 섹션별로 나누고, **BAAI/bge-m3의 1024차원 벡터**를 PostgreSQL의
`doc_chunks` 한 테이블에 저장합니다. FastAPI와 MCP 서버를 한 프로세스에서 실행합니다.
MCP의 `rag_search_docs` 툴로 저장된 문서를 검색합니다.

## 남긴 파일

```text
app/
├── main.py        # 직접 실행하는 서버 진입점: FastAPI + MCP
├── mcp_server.py  # rag_search_docs 등록과 내부 API 키 검증
├── config.py      # .env 설정
├── schemas.py     # 업로드 메타데이터와 응답
├── ingest.py      # Markdown 읽기, 섹션 분할, 추가 본문 병합, SHA-256
├── embeddings.py  # bge-m3 직접 실행, 1024차원 검증·정규화
└── database.py    # pgvector 저장·검색, 변경 청크 재임베딩
sql/doc_chunks.sql # 제공된 테이블과 HNSW 인덱스
tests/             # Markdown·API·임베딩 검증, 선택 실행하는 실제 DB 검사
```

## 설정과 실행

전달받은 접속값은 로컬 `.env`에 설정했습니다. 이 파일은 Git에서 제외하고,
`.env.example`에는 비밀값 없이 형식만 남겼습니다.

- `DB_*`: 문서 저장·검색에 사용하는 Supabase 연결 설정. `DB_USER`에 지정된 계정으로 접속합니다.
- `DB_SSLMODE=require`: 앱에서 Supabase pooler까지 SSL 연결을 요구합니다.
- `DB_SCHEMA=public,extensions`: 연결 후 PostgreSQL `search_path`에 적용합니다. `extensions`에 설치된 pgvector 타입을 찾는 데 필요합니다.
- `RAG_DB_*`: 향후 조회 권한을 분리할 때 사용할 설정. 현재 업로드·검색에는 사용하지 않습니다.
- `POSTGRES_ADMIN_*`: 전달받은 관리용 설정을 보관하지만 앱에서는 읽거나 사용하지 않습니다.
- `RABBITMQ_*`: 접속값만 설정했습니다. 현재 업로드는 동기 처리이며 큐·Worker는 추가하지 않았습니다.
- `INTERNAL_API_KEY`: `/internal/documents`와 `/mcp`의 `X-Internal-Api-Key` 헤더 검증에 사용합니다.

현재 DB 주소는 Supabase의 **session pooler(5432)**입니다. 문서별 세션 잠금을 사용하므로
transaction pooler(6543)로 포트만 바꿔 사용하지 마세요.
연결된 DB에는 `extensions.vector` 0.8.2, `public.doc_chunks`의 `vector(1024)`와
HNSW 인덱스가 준비되어 있음을 확인했습니다.
`rag_reader` 역할은 아직 없으며, 향후 조회 계정으로 사용하려면 DB 역할·권한과 pooler용 로그인명을 별도로 준비해야 합니다.
접속 방식은 [Supabase 공식 문서](https://supabase.com/docs/guides/database/connecting-to-postgres)를 참고하세요.

Python 3.11 이상에서:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app/main.py
```

실행 파일은 `app/main.py`입니다. 파일을 직접 실행하면 FastAPI와 MCP 서버가 시작됩니다.
실행 중인 터미널을 유지하고 종료할 때 Ctrl+C를 누릅니다.
같은 실행에서 다음 주소가 함께 열립니다.

| 용도 | 주소 |
| --- | --- |
| MCP — Streamable HTTP | `http://127.0.0.1:8102/mcp` |
| MD 업로드 | `POST http://127.0.0.1:8102/internal/documents` |
| API 문서 | `http://127.0.0.1:8102/docs` |
| 프로세스 상태 | `http://127.0.0.1:8102/health` |

MCP 클라이언트에는 `/mcp` URL과 `X-Internal-Api-Key` 헤더를 지정합니다.
키 값은 `.env`의 `INTERNAL_API_KEY`입니다. MCP는 일반 웹페이지가 아니므로 브라우저 대신 MCP 클라이언트로 연결합니다.
`initialize`, `ping`, `tools/list`가 동작하며 Tool 목록에는 `rag_search_docs`가 등록되어 있습니다.
MD 업로드는 REST로 처리하고, 검색은 MCP 툴로 호출합니다.
MCP 서버 실행만으로 DB 접속이나 모델 다운로드를 시작하지 않습니다.

MCP 서버는 실행 중이어야 합니다. 이 프로젝트는 [공식 SDK의 ASGI 통합 방식](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/docs/server.md)을 사용하므로,
위 명령 하나로 FastAPI와 MCP를 함께 실행합니다. Agent 애플리케이션은 별도로 실행해 이 서버에 연결합니다.

Swagger: http://127.0.0.1:8102/docs — **Authorize**에 내부 API 키를 입력하세요.
`/health`는 프로세스 상태만 반환합니다. DB나 모델 준비 완료를 의미하지 않습니다.

PostgreSQL에 pgvector 확장과 테이블이 준비되어 있어야 합니다.
기존 `doc_chunks`가 있으면 그대로 사용합니다. 새로 구성할 경우 DBA가 pgvector를 설치·활성화한 뒤,
테이블 생성 권한이 있는 계정으로 다음 SQL을 적용하세요. 앱이 시작하면서 DDL을 실행하지는 않습니다.

```bash
psql "host=aws-0-ap-southeast-2.pooler.supabase.com port=5432 dbname=postgres user=postgres.YOUR_PROJECT_REF sslmode=require" -f sql/doc_chunks.sql
```

새 DB 구성 시 위 명령의 `YOUR_PROJECT_REF`를 실제 프로젝트 참조값으로 바꿉니다.
현재 연결된 DB에는 테이블이 이미 있어 SQL을 다시 실행하지 않았습니다.
앱 계정은 `doc_chunks`에 SELECT/INSERT/UPDATE/DELETE 권한이 필요합니다.
업무 테이블에는 접근하지 않습니다.

## Markdown 업로드

`POST /internal/documents`에 **multipart/form-data**로 두 필드를 보냅니다.

| 필드 | 내용 |
| --- | --- |
| `file` | UTF-8 Markdown 파일(`.md` 또는 `.markdown`), 기본 최대 20MB |
| `metadata` | 아래 형식의 JSON 문자열 |

```json
{
  "doc_type": "design_std",
  "source": "design_standard.md",
  "additional_sections": [
    {
      "section": "7. 커넥터와 냉각수",
      "content": "고객에게 확인한 냉각수 종류는 에틸렌글리콜 50% 혼합액이다."
    }
  ]
}
```

- `doc_type`은 필수: `design_std`, `catalog`, `policy`, `alt_parts`.
- `source`는 선택이며 생략하면 업로드 파일명을 사용합니다. DB 제약에 따라 최대 50자입니다.
- `additional_sections`는 문서에 없는 추가 정보입니다. 같은 섹션 제목이면 기존 본문 뒤에 붙이고, 새 제목이면 새 청크를 만듭니다.

제공된 문서는 각각 다음 `doc_type`으로 업로드합니다.

| 파일 | `doc_type` | 섹션 수 |
| --- | --- | --- |
| `design_standard.md` | `design_std` | 7 |
| `product_catalog.md` | `catalog` | 6 |
| `sales_policy.md` | `policy` | 10 |
| `alt_parts_criteria.md` | `alt_parts` | 3 |

다음 예시는 쉘에 `INTERNAL_API_KEY` 환경 변수가 설정된 상태에서 실행합니다.
서버의 `.env` 로딩은 별도 터미널의 환경 변수를 설정하지 않습니다.

```bash
curl -X POST http://127.0.0.1:8102/internal/documents \
  -H "X-Internal-Api-Key: ${INTERNAL_API_KEY}" \
  -F 'file=@/path/to/design_standard.md;type=text/markdown' \
  -F 'metadata={"doc_type":"design_std"}'
```

성공 응답 예시:

```json
{
  "doc_type": "design_std",
  "source": "design_standard.md",
  "chunk_count": 7,
  "embedded_chunks": 7,
  "unchanged_chunks": 0,
  "embedding_status": "DONE",
  "embedding_model": "BAAI/bge-m3"
}
```

## 추출·저장 동작

1. UTF-8 Markdown을 읽고 `## 1. 제목`, `## 제10조 제목`처럼 `##`~`######` 제목을 기준으로 나눕니다. 해당 제목이 없는 문서는 `#` 문서 제목을 제외한 내용을 `본문`으로 처리합니다.
2. 섹션 제목이 있으면 문서 제목·서문은 제외합니다. 본문의 목록·표·줄바꿈은 보존하고, 코드 블록 안의 제목은 섹션으로 나누지 않습니다.
3. 추가 내용을 병합하고 긴 섹션을 1,600자씩, 150자를 겹쳐 나눕니다. 최종 청크 본문으로 SHA-256을 계산합니다.
4. 변경된 청크를 `PENDING`으로 저장하고 이전 벡터를 비웁니다.
5. bge-m3로 임베딩하고 1024차원·유한값·0 벡터 여부를 검사한 뒤 L2 정규화합니다.
6. 벡터와 모델명·임베딩 시간을 저장하고 `DONE`으로 바꿉니다. 모델 처리 실패 시 `FAILED`로 기록하고 HTTP 503을 반환합니다.

`doc_type + source`가 같은 업로드는 **문서 전체 교체**입니다.
같은 섹션의 본문 해시·모델이 같고 기존 벡터가 `DONE`이면 재사용합니다.
새 문서에서 빠진 섹션은 삭제하므로, 재업로드 시 유지할 수동 보충 정보도 다시 보내세요.
`FAILED` 또는 중단된 `PENDING` 청크는 같은 문서 재업로드로 재처리합니다.

새 청크 ID는 문서·섹션을 기준으로 생성하며 30자 이내입니다.
같은 `source`와 섹션으로 기존 시드 데이터가 있으면 `dsn-001` 같은 ID를 유지합니다.
제공된 MD 파일명을 그대로 사용하면 기존 시드의 `source`와 일치합니다.

첫 임베딩 시 모델을 다운로드해 `.cache/models`에 저장합니다. 초기 다운로드에는 네트워크와
모델 저장 공간이 필요하고, 기본 실행 장치는 CPU입니다. `EMBEDDING_DEVICE`로 `cuda` 또는 `mps`를 선택할 수 있습니다.
모델을 프로세스마다 로드하므로 우선 worker 1개로 실행하세요.

Markdown 안의 이미지나 링크 대상은 가져오지 않습니다. 필요한 설명은 `additional_sections`로 보완하세요.
원본 파일 자체는 보관하지 않고 섹션 본문과 벡터를 저장합니다.

참고: [bge-m3 모델](https://huggingface.co/BAAI/bge-m3),
[pgvector Python 연동](https://github.com/pgvector/pgvector-python).

## MCP 검색: rag_search_docs

MCP 클라이언트에서 `http://127.0.0.1:8102/mcp`에 내부 API 키를 설정하고
`rag_search_docs`를 다음 인자로 호출합니다.

```json
{
  "query": "고객 요구 보증 5년 15만km",
  "doc_type": "policy",
  "k": 4
}
```

- `query`: 필수, 공백을 제외한 질문 1~2,000자.
- `doc_type`: 필수, `design_std`, `catalog`, `policy`, `alt_parts` 중 하나.
- `k`: 반환할 최대 청크 수, 기본 4, 허용 범위 1~20.

업로드와 같은 bge-m3 인스턴스로 질문을 1024차원 벡터로 만들고 `doc_chunks`를 직접 조회합니다.
해당 문서 유형에서 `embedding IS NOT NULL`, `embedding_status = 'DONE'`,
`embedding_model = 'BAAI/bge-m3'`인 청크만 검색합니다.
`score`는 `1 - 코사인 거리`이며 -1~1 범위에서 클수록 유사합니다.
확률이나 정답 신뢰도는 아닙니다. [pgvector 검색 연산](https://github.com/pgvector/pgvector#querying)

결과 배열의 각 항목은 `chunk_id`, `section`, `content`, `score`입니다.
실제 응답에서는 `structuredContent.result`에 배열을 한 번만 담고 `content`는 빈 배열로 반환합니다.
클라이언트는 `structuredContent`를 읽어야 합니다. 텍스트 `content`만 읽는 구형 클라이언트는 지원하지 않습니다.
검색 가능한 청크가 없으면 빈 배열이며, 먼저 해당 유형의 문서를 업로드해 임베딩을 완료해야 합니다.
질문 임베딩이나 DB 연결이 실패하면 MCP 응답의 `isError`가 `true`이며, 오류 메시지는 `content`에 담깁니다.

Postman의 일반 HTTP 요청으로 테스트할 경우 `POST http://127.0.0.1:8102/mcp`에
다음 헤더와 **Body → raw → JSON**을 사용합니다.

| 헤더 | 값 |
| --- | --- |
| `X-Internal-Api-Key` | `.env`의 `INTERNAL_API_KEY` |
| `Content-Type` | `application/json` |
| `Accept` | `application/json, text/event-stream` |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "rag_search_docs",
    "arguments": {
      "query": "고객 요구 보증 5년 15만km",
      "doc_type": "policy",
      "k": 4
    }
  }
}
```

위 HTTP 응답의 검색 배열 위치는 `result.structuredContent.result`입니다.
`design_standard.md`만 업로드했다면 `doc_type`을 `design_std`,
`query`를 `고온 옵션의 최고 작동 온도`로 바꿔 테스트하세요.

## 검증

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
# PostgreSQL/pgvector 및 테이블을 준비한 뒤 실제 DB 검사
RUN_DB_TESTS=1 python -m pytest tests/test_database.py -q
```

기본 테스트는 Markdown 섹션 분할과 API·입력·벡터 검증을 수행합니다.
MCP 초기화·ping·Tool 목록, 검색 호출·입력 검증·오류 처리와 내부 API 키 검증도 실제 SDK로 검사합니다.
DB 테스트는 고유한 테스트 문서만 저장·정리하며, 테스트용 벡터로 저장·재사용·실패·재시도를 확인합니다.
실제 모델 추론 테스트와는 별개입니다.

제공된 Markdown 4개의 섹션 분할 결과 26개와 본문 해시가 제공된 데이터셋과 일치함을 확인했습니다.
