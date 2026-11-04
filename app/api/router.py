from fastapi import APIRouter

from app.api.routes import chat, engine, evaluations, standards

api_router = APIRouter()
api_router.include_router(standards.router)
api_router.include_router(chat.router)
api_router.include_router(evaluations.router)
api_router.include_router(engine.router)