from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.api.deps import get_evaluation_controller
from app.controller.evaluation_controller import EvaluationController
from app.schema.evaluation import EvaluationOut

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post("", response_model=EvaluationOut)
async def create_evaluation(
    user_id: str = Form(...),
    report_file: UploadFile | None = File(None, description="Report PDF (or use report_id / chat_id)"),
    report_id: str | None = Form(None, description="Already uploaded report (report.id from /chat)"),
    chat_id: str | None = Form(None, description="Use the chat's active report + standards"),
    standard_files: list[UploadFile] | None = File(None, description="Max 2; Part 1 parent added automatically"),
    standard_ids: str | None = Form(None, description="'id1,id2'"),
    use_cache: bool = Form(True, description="false -> run the AI again"),
    analysis_id: str | None = Form(None, description="Cheap mode: build Excel/PDF from an existing /chat "
                                                       "answer (analysis_id) without re-sending the PDFs"),
    ctrl: EvaluationController = Depends(get_evaluation_controller),
):
    """Full technical evaluation -> Excel (golden layout) + branded PDF. Takes 1-5 minutes."""
    return await ctrl.create(user_id=user_id, report_file=report_file, report_id=report_id, chat_id=chat_id,
                       standard_files=standard_files, standard_ids=standard_ids, use_cache=use_cache,
                       analysis_id=analysis_id)


@router.post("/from-analysis", response_model=EvaluationOut)
async def evaluation_from_analysis(
    user_id: str = Form(...),
    analysis_id: str | None = Form(None, description="analysis_id from a /chat response"),
    report_id: str | None = Form(None, description="Needed only with markdown_file"),
    markdown_file: UploadFile | None = File(None, description="Optional: a saved outputs/*.md answer"),
    ctrl: EvaluationController = Depends(get_evaluation_controller),
):
    """Excel + PDF from an existing chat answer - NO AI call, no OpenAI cost."""
    return await ctrl.create_from_markdown(user_id=user_id, analysis_id=analysis_id, report_id=report_id,
                                     markdown_file=markdown_file)


@router.get("", response_model=list[EvaluationOut])
async def list_evaluations(user_id: str = Query(...), limit: int = Query(50, ge=1, le=200),
                     ctrl: EvaluationController = Depends(get_evaluation_controller)):
    return await ctrl.list_all(user_id, limit)


@router.get("/{evaluation_id}", response_model=EvaluationOut)
async def get_evaluation(evaluation_id: str, user_id: str | None = Query(None),
                   ctrl: EvaluationController = Depends(get_evaluation_controller)):
    return await ctrl.get(evaluation_id, user_id)


@router.get("/{evaluation_id}/download/{fmt}")
async def download_evaluation(evaluation_id: str, fmt: str, user_id: str | None = Query(None),
                        ctrl: EvaluationController = Depends(get_evaluation_controller)):
    """fmt = pdf | xlsx"""
    return await ctrl.download(evaluation_id, fmt, user_id)


@router.post("/{evaluation_id}/render", response_model=EvaluationOut)
async def rerender_evaluation(evaluation_id: str, user_id: str = Form(...),
                        ctrl: EvaluationController = Depends(get_evaluation_controller)):
    """Re-create PDF/XLSX from the stored result without calling the AI (after changing the format)."""
    return await ctrl.rerender(evaluation_id, user_id)
