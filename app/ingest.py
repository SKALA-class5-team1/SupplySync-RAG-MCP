import hashlib
import re

from app.schemas import Chunk, Section, UploadMetadata

HEADING = re.compile(r"^ {0,3}#{2,6}[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
PREFIX = {"design_std": "dsn", "catalog": "cat", "policy": "pol", "alt_parts": "alt"}


def split_sections(text: str) -> list[Section]:
    """Markdown의 ##~###### 제목으로 나누고 본문의 목록·표·줄바꿈을 보존합니다."""
    sections: list[Section] = []
    heading = None
    body: list[str] = []
    preamble: list[str] = []
    fence = ""

    def append_section() -> None:
        content = "\n".join(body).strip()
        if heading and content:
            sections.append(Section(section=heading, content=content))

    for line in text.splitlines():
        marker = FENCE.match(line)
        if fence:
            if (marker and marker[1][0] == fence[0]
                    and len(marker[1]) >= len(fence) and not marker[2].strip()):
                fence = ""
            (body if heading is not None else preamble).append(line)
            continue
        if marker:
            fence = marker[1]
            (body if heading is not None else preamble).append(line)
            continue

        match = HEADING.match(line)
        if match:
            append_section()
            heading = re.sub(r"[ \t]+#+[ \t]*$", "", match[1]).strip()
            body = []
        elif heading is not None:
            body.append(line)
        elif not re.match(r"^ {0,3}#(?:[ \t]+|$)", line):
            preamble.append(line)
    append_section()
    if heading is None:
        content = "\n".join(preamble).strip()
        if content:
            sections.append(Section(section="본문", content=content))
    return sections


def prepare_chunks(data: bytes, filename: str, metadata: UploadMetadata) -> tuple[str, list[Chunk]]:
    if not filename.lower().endswith((".md", ".markdown")):
        raise ValueError("Markdown 파일(.md 또는 .markdown)을 업로드하세요.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Markdown 문서는 UTF-8 인코딩으로 저장하세요.") from exc
    if "\x00" in text:
        raise ValueError("Markdown 문서에 사용할 수 없는 NUL 문자가 있습니다.")

    # 파일명만 저장합니다. 클라이언트 경로를 로컬 파일 경로로 사용하지 않습니다.
    source = metadata.source or re.split(r"[/\\]", filename)[-1]
    if not source.strip() or len(source) > 50:
        raise ValueError("원본 파일명은 1~50자여야 합니다. metadata.source로 지정할 수 있습니다.")
    sections = split_sections(text)
    merged: dict[str, str] = {}
    for item in [*sections, *metadata.additional_sections]:
        # 같은 섹션에 수동 보충 내용을 덧붙여 함께 임베딩합니다.
        merged[item.section] = "\n".join(filter(None, [merged.get(item.section), item.content]))

    chunks = []
    for section, content in merged.items():
        for part, start in enumerate(range(0, len(content), 1450), start=1):
            text = content[start:start + 1600].strip()
            if not text:
                continue
            title = section if part == 1 else f"{section} ({part})"
            if len(title) > 50:
                raise ValueError(f"분할된 섹션 제목은 50자 이하여야 합니다: {title}")
            identity = f"{metadata.doc_type}\0{source}\0{title}"
            chunk_id = PREFIX[metadata.doc_type] + "-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            chunks.append(Chunk(
                chunk_id=chunk_id, section=title, content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            ))
            if start + 1600 >= len(content):
                break
    if not chunks:
        raise ValueError("저장할 본문이 없습니다. Markdown 본문 또는 additional_sections를 제공하세요.")
    if len({chunk.section for chunk in chunks}) != len(chunks):
        raise ValueError("분할된 섹션 제목이 중복됩니다. 원문 섹션 제목을 구분하세요.")
    return source, chunks
