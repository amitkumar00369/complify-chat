from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.api.deps import get_chat_controller
from app.controller.chat_controller import ChatController
from app.schema.chat import ChatMessageOut, ChatResponse, ChatSessionOut, ReportOut

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    user_id: str = Form(..., description="Your user id (no auth yet)"),
    query: str = Form(..., description="User prompt"),
    chat_id: str | None = Form(None, description="Empty -> new chat is created"),
    report_file: UploadFile | None = File(None, description="Report PDF (max 1)"),
    standard_files: list[UploadFile] | None = File(None, description="Standard PDFs (max 2)"),
    standard_ids: str | None = Form(None, description="Already-uploaded standard ids: 'id1,id2'"),
    use_cache: bool = Form(True, description="false -> always call the AI"),
    reuse_report_output: bool = Form(True, description="Same report evaluated before -> return that "
                                                         "evaluation (no AI). false -> evaluate again"),
    ctrl: ChatController = Depends(get_chat_controller),
):
    """Chat. An evaluation answer also produces Excel + PDF (field `evaluation`) without an extra AI call."""
    return await ctrl.chat(
        user_id=user_id, query=query, chat_id=chat_id, report_file=report_file,
        standard_files=standard_files, standard_ids=standard_ids, use_cache=use_cache,
        reuse_report_output=reuse_report_output,
    )


@router.get("/chats", response_model=list[ChatSessionOut])
async def list_chats(
    user_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctrl: ChatController = Depends(get_chat_controller),
):
    return await ctrl.list_sessions(user_id, limit, offset)


@router.get("/chats/{chat_id}/messages", response_model=list[ChatMessageOut])
async def chat_messages(chat_id: str, user_id: str = Query(...),
                  ctrl: ChatController = Depends(get_chat_controller)):
    return await ctrl.list_messages(chat_id, user_id)


@router.get("/reports", response_model=list[ReportOut])
async def list_reports(
    user_id: str = Query(...),
    chat_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    ctrl: ChatController = Depends(get_chat_controller),
):
    return await ctrl.list_reports(user_id, chat_id, limit)
