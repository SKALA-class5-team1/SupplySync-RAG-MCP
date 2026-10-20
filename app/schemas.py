from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DocType = Literal["design_std", "catalog", "policy", "alt_parts"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    section: Name
    content: str = Field(min_length=1, max_length=50000)


class UploadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type: DocType
    source: Name | None = None
    additional_sections: list[Section] = Field(default_factory=list, max_length=100)


@dataclass
class Chunk:
    chunk_id: str
    section: str
    content: str
    content_hash: str


class UploadResult(BaseModel):
    doc_type: DocType
    source: str
    chunk_count: int
    embedded_chunks: int
    unchanged_chunks: int
    embedding_status: Literal["DONE"] = "DONE"
    embedding_model: str


class SearchHit(BaseModel):
    chunk_id: str
    section: str
    content: str
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
