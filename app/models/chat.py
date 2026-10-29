from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.base import new_id, utcnow


class ChatSession(Base):
    """A chat thread owned by a user (user_id comes from the client for now)."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(255))
    openai_conversation_id: Mapped[str | None] = mapped_column(String(100))
    active_report_file_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("report_files.id")
    )
    active_standard_ids: Mapped[list] = mapped_column(JSON, default=list)
    # OpenAI file ids already sent into this conversation (never re-send them)
    attached_openai_file_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatMessage(Base):
    """Every user / assistant turn is a new row."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    report_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("reports.id"))
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
