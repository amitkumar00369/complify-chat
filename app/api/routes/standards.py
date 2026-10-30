from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.api.deps import get_standard_controller
from app.controller.standard_controller import StandardController
from app.schema.standard import StandardFamilyOut, StandardOut, StandardUploadResponse

router = APIRouter(prefix="/standards", tags=["standards"])


@router.post("", response_model=StandardUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_standard(
    response: Response,
    file: UploadFile = File(..., description="Standard PDF (Part 1 or Part 2-x)"),
    name: str | None = Form(None, description="e.g. 'IEC 60335-2-7'. Auto-detected if empty"),
    edition: str | None = Form(None, description="e.g. '2012'. Auto-detected if empty"),
    ctrl: StandardController = Depends(get_standard_controller),
):
    result, created = await ctrl.upload(file, name, edition)
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("", response_model=list[StandardOut])
async def list_standards(
    family: str | None = Query(None, description="e.g. 'IEC 60335'"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctrl: StandardController = Depends(get_standard_controller),
):
    return await ctrl.list_all(limit, offset, family)


@router.get("/tree", response_model=list[StandardFamilyOut])
async def standards_tree(ctrl: StandardController = Depends(get_standard_controller)):
    """Family -> part1 / part2 / other, same as the folder layout."""
    return await ctrl.tree()


@router.get("/{standard_id}", response_model=StandardUploadResponse)
async def get_standard(standard_id: str, ctrl: StandardController = Depends(get_standard_controller)):
    return await ctrl.get(standard_id)
