from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChatMessage, ChatSession
from app.utils.exceptions import ForbiddenError, NotFoundError
from app.utils.text_utils import make_title


class ChatService:
    def __init__(self, db: Session):
        self.db = db

    def get_owned(self, chat_id: str, user_id: str) -> ChatSession:
        chat = self.db.get(ChatSession, chat_id)
        if not chat:
            raise NotFoundError(f"Chat '{chat_id}' not found")
        if chat.user_id != user_id:
            raise ForbiddenError("This chat belongs to another user")
        return chat

    def get_or_create(self, user_id: str, chat_id: str | None, first_query: str) -> tuple[ChatSession, bool]:
        """chat_id given -> continue that chat. Not given -> create a new chat."""
        if chat_id:
            return self.get_owned(chat_id, user_id), False
        chat = ChatSession(user_id=user_id, title=make_title(first_query),
                           active_standard_ids=[], attached_openai_file_ids=[])
        self.db.add(chat)
        self.db.commit()
        return chat, True

    def add_message(self, chat: ChatSession, role: str, content: str, *,
                    report_id: str | None = None, from_cache: bool = False,
                    attachments: list[str] | None = None) -> ChatMessage:
        msg = ChatMessage(chat_id=chat.id, role=role, content=content, report_id=report_id,
                          from_cache=from_cache, attachments=attachments or [])
        self.db.add(msg)
        return msg

    def list_sessions(self, user_id: str, limit: int = 50, offset: int = 0) -> list[ChatSession]:
        stmt = (select(ChatSession).where(ChatSession.user_id == user_id)
                .order_by(ChatSession.updated_at.desc()).limit(limit).offset(offset))
        return list(self.db.scalars(stmt))

    def list_messages(self, chat_id: str, user_id: str) -> list[ChatMessage]:
        self.get_owned(chat_id, user_id)
        stmt = (select(ChatMessage).where(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.created_at, ChatMessage.role.desc()))
        return list(self.db.scalars(stmt))
