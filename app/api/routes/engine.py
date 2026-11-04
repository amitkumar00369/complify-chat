"""Evaluation from the compliance engine's JSON files (no PDFs).

POST /engine-evaluations                    effective_kb + report_evidence -> PDF / XLSX / JSON / MD
GET  /engine-evaluations/{id}               same response again
GET  /engine-evaluations/{id}/download/{fmt}   fmt = pdf | json | md | xlsx
"""
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from app.api.deps import get_ai_dep, get_storage_dep
from app.service.ai_service import AIService
from app.service.engine_evaluation import EngineEvaluationService
from app.service.storage_service import StorageBackend

router = APIRouter(prefix="/engine-evaluations", tags=["engine evaluations"])


def get_engine_service(storage: StorageBackend = Depends(get_storage_dep),
                       ai: AIService = Depends(get_ai_dep)) -> EngineEvaluationService:
    return EngineEvaluationService(storage, ai)


@router.post("")
async def create_engine_evaluation(
    effective_kb: UploadFile = File(..., description="standards/effective_kb.json from the engine run"),
    report_evidence: UploadFile = File(..., description="report/report_evidence.json from the engine run"),
    use_cache: bool = Form(True, description="Same two files again -> stored result is returned"),
    svc: EngineEvaluationService = Depends(get_engine_service),
):
    """Technical evaluation from the engine's JSON. Output files have the same structure as
    /evaluations: PDF, Excel, gap JSON and the markdown evaluation."""
    return await svc.evaluate(await effective_kb.read(), await report_evidence.read(), use_cache=use_cache)


@router.get("/{evaluation_id}")
async def get_engine_evaluation(evaluation_id: str, svc: EngineEvaluationService = Depends(get_engine_service)):
    return await svc.get(evaluation_id)


@router.get("/{evaluation_id}/download/{fmt}")
async def download_engine_evaluation(evaluation_id: str, fmt: str,
                                     svc: EngineEvaluationService = Depends(get_engine_service)):
    data, name, media = await svc.file(evaluation_id, fmt)
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                     "Cache-Control": "no-store"})