from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.base import new_id, utcnow


class ReportFile(Base):
    """An uploaded report PDF (deduplicated by SHA-256).

    Disk: reports/{id}/{original_filename}  +  reports/{id}/outputs/{analysis_id}.md
    """

    __tablename__ = "report_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    original_filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # ["IEC 60335-2-7:2012", "IEC 60335-1:2010"] in order of appearance
    detected_standards: Mapped[list] = mapped_column(JSON, default=list)
    openai_file_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    """One analysis: report + standards used + prompt + AI output.

    This table is the response cache: the same report + same standards + same
    (normalised) prompt is answered from here without calling the AI API.
    """

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    chat_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    report_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("report_files.id"), index=True
    )
    report_name: Mapped[str] = mapped_column(String(500))
    standard_ids: Mapped[list] = mapped_column(JSON, default=list)
    standard_names: Mapped[list] = mapped_column(JSON, default=list)
    standards_key: Mapped[str] = mapped_column(String(500), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    prompt_normalized: Mapped[str] = mapped_column(Text, index=True)
    output: Mapped[str] = mapped_column(Text)
    output_path: Mapped[str | None] = mapped_column(String(1000))  # reports/{rf_id}/outputs/{id}.md
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
