from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.base import new_id, utcnow


class Evaluation(Base):
    """Full structured evaluation of one report against its standards (golden-workbook format).

    The structured result is kept in the DB (JSON column); on disk there are only the
    rendered files: reports/{report_file_id}/outputs/evaluation_{id}/*.pdf|*.xlsx
    """

    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("chat_sessions.id", ondelete="SET NULL"))
    report_file_id: Mapped[str] = mapped_column(String(36), ForeignKey("report_files.id"), index=True)
    standard_ids: Mapped[list] = mapped_column(JSON, default=list)
    standard_names: Mapped[list] = mapped_column(JSON, default=list)
    standards_key: Mapped[str] = mapped_column(String(500), index=True)
    status: Mapped[str] = mapped_column(String(20), default="completed")
    final_verdict: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[dict | None] = mapped_column(JSON)          # EvaluationDocument
    pdf_path: Mapped[str | None] = mapped_column(String(1000))
    xlsx_path: Mapped[str | None] = mapped_column(String(1000))
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
