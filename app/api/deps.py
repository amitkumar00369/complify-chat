from fastapi import Depends
from sqlalchemy.orm import Session

from app.controller.chat_controller import ChatController
from app.controller.evaluation_controller import EvaluationController
from app.controller.standard_controller import StandardController
from app.db.database import get_db
from app.service.ai_service import AIService, get_ai_service
from app.service.storage_service import StorageBackend, get_storage


def get_storage_dep() -> StorageBackend:
    return get_storage()


def get_ai_dep() -> AIService:
    return get_ai_service()


def get_standard_controller(
    db: Session = Depends(get_db), storage: StorageBackend = Depends(get_storage_dep)
) -> StandardController:
    return StandardController(db, storage)


def get_chat_controller(
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_dep),
    ai: AIService = Depends(get_ai_dep),
) -> ChatController:
    return ChatController(db, storage, ai)


def get_evaluation_controller(
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_dep),
    ai: AIService = Depends(get_ai_dep),
) -> EvaluationController:
    return EvaluationController(db, storage, ai)
