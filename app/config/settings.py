"""Application settings, loaded from the `.env` file in the project root."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- app ---
    app_name: str = "Complify Chat API"
    environment: str = "dev"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # --- database (SQLite file lives OUTSIDE app/, in ./data) ---
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'complify.db').as_posix()}"

    # --- storage (local for now; s3 kept ready for later) ---
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: Path = BASE_DIR / "storage"
    standards_prefix: str = "standards/"
    reports_prefix: str = "reports/"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "ap-south-1"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # e.g. MinIO / LocalStack

    # --- OpenAI ---
    openai_api_key: str | None = None
    openai_model: str = "gpt-6-astra"
    openai_timeout_seconds: float = 300.0
    openai_max_output_tokens: int = 8000
    eval_max_output_tokens: int = 32000   # structured evaluation is large

    # --- evaluation report branding (your own PDF format) ---
    brand_name: str = "Complify"
    brand_tagline: str = "Compliance Engineering & Technical Evaluation"
    brand_color: str = "#086457"          # same teal as the client workbook
    brand_logo_path: str | None = None    # optional PNG/JPG for the PDF cover
    evaluation_prepared_by: str = "Complify Evaluation Engine"

    # --- upload limits ---
    max_upload_mb: int = 50
    max_standards_per_request: int = 2   # standard PDFs/ids the user can send at once
    max_standards_in_context: int = 3    # after adding Part 1 parents automatically

    # --- rate limiting ---
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60

    # --- response cache ---
    cache_enabled: bool = True
    # 1.0 = exact match after normalisation. Lower (e.g. 0.95) allows fuzzy
    # matches, but "clause 10.1" vs "clause 10.2" would ALSO match — be careful.
    cache_similarity_threshold: float = Field(default=1.0, ge=0.5, le=1.0)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
