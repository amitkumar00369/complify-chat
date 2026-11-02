from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.api.deps import get_chat_controller
from app.controller.chat_controller import ChatController
from app.schema.chat import ChatMessageOut, ChatPublicResponse, ChatResponse, ChatSessionOut, ReportOut

router = APIRouter(tags=["Report-Analysis"])


_FORM = dict(
    query_default="Evaluate this report against the applicable standards",
)


async def _run_chat(ctrl, user_id, query, chat_id, report_file, standard_files, standard_ids, use_cache,
                    reuse_report_output) -> ChatResponse:
    return await ctrl.chat(
        user_id=user_id, query=query, chat_id=chat_id, report_file=report_file,
        standard_files=standard_files, standard_ids=standard_ids, use_cache=use_cache,
        reuse_report_output=reuse_report_output,
    )


@router.post("/report-analysis", response_model=ChatPublicResponse)
async def chat(
    user_id: str = Form(default="user", description="Your user id (no auth yet)"),
    query: str = Form(default=_FORM["query_default"], description="User prompt"),
    session_id: str | None = Form(None, description="Empty -> new chat session is created"),
    chat_id: str | None = Form(None, description="Old name of session_id (still accepted)"),
    report_file: UploadFile | None = File(None, description="Report PDF (max 1)"),
    standard_files: list[UploadFile] | None = File(None, description="Standard PDFs (max 2)"),
    standard_ids: str | None = Form(None, description="Already-uploaded standard ids: 'id1,id2'"),
    use_cache: bool = Form(True, description="false -> always call the AI"),
    reuse_report_output: bool = Form(True, description="Same report evaluated before -> return that "
                                                         "evaluation (no AI). false -> evaluate again"),
    ctrl: ChatController = Depends(get_chat_controller),
):
    """Evaluation -> result = {gaps, final_summary}, evaluation = {download_json, download_pdf}.
    Follow-up question -> answer = text, result/evaluation = null."""
    full = await _run_chat(ctrl, user_id, query, session_id or chat_id, report_file, standard_files, standard_ids,
                           use_cache, reuse_report_output)
    return ChatPublicResponse.from_full(full)


@router.post("/report-analysis/full", response_model=ChatResponse)
async def chat_full(
    user_id: str = Form(...),
    query: str = Form(default=_FORM["query_default"]),
    chat_id: str | None = Form(None),
    report_file: UploadFile | None = File(None),
    standard_files: list[UploadFile] | None = File(None),
    standard_ids: str | None = Form(None),
    use_cache: bool = Form(True),
    reuse_report_output: bool = Form(True),
    ctrl: ChatController = Depends(get_chat_controller),
):
    """Every detail (chat_id, warnings, evaluation ids and paths, report_id) - for debugging and admin tools."""
    return await _run_chat(ctrl, user_id, query, chat_id, report_file, standard_files, standard_ids, use_cache,
                           reuse_report_output)


@router.get("/report-analysises", response_model=list[ChatSessionOut])
async def list_chats(
    user_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctrl: ChatController = Depends(get_chat_controller),
):
    return await ctrl.list_sessions(user_id, limit, offset)


@router.get("/report-analysis/{chat_id}/messages", response_model=list[ChatMessageOut])
async def chat_messages(chat_id: str, user_id: str = Query(...),
                  ctrl: ChatController = Depends(get_chat_controller)):
    return await ctrl.list_messages(chat_id, user_id)


@router.get("/report-analysis", response_model=list[ReportOut])
async def list_reports(
    user_id: str = Query(...),
    chat_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    ctrl: ChatController = Depends(get_chat_controller),
):
    return await ctrl.list_reports(user_id, chat_id, limit)