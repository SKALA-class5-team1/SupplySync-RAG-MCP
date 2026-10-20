from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True,
    )

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "supplysync"
    db_user: str = "supplysync_app"
    db_password: SecretStr
    db_sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "require"
    db_schema: str = "public,extensions"
    rag_db_user: str = "rag_reader"
    rag_db_password: SecretStr

    rabbitmq_host: str = "localhost"
    rabbitmq_amqp_port: int = 5672
    rabbitmq_mgmt_port: int = 15672
    rabbitmq_user: str = "supplysync"
    rabbitmq_password: SecretStr

    internal_api_key: SecretStr = Field(min_length=1)
    embedding_model: Literal["BAAI/bge-m3"] = "BAAI/bge-m3"
    embedding_device: Literal["cpu", "cuda", "mps"] = "cpu"
    embedding_cache_dir: Path = ROOT / ".cache" / "models"
    max_upload_mb: int = Field(default=20, ge=1, le=100)
