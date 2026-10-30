from collections import OrderedDict

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models import Standard
from app.schema.standard import StandardFamilyOut, StandardOut, StandardRefOut, StandardUploadResponse
from app.service.standard_service import StandardService
from app.service.storage_service import StorageBackend
from app.utils.pdf_utils import read_pdf_upload


class StandardController:
    def __init__(self, db: AsyncSession, storage: StorageBackend):
        self.service = StandardService(db, storage)
        self.settings = get_settings()

    async def _response(self, std: Standard, duplicate: bool, warnings: list[str]) -> StandardUploadResponse:
        parent = await self.service.parent(std)
        return StandardUploadResponse(
            standard=StandardOut.model_validate(std),
            duplicate=duplicate,
            parent=StandardRefOut.model_validate(parent) if parent else None,
            children=[StandardRefOut.model_validate(c) for c in await self.service.children(std)],
            warnings=warnings,
        )

    async def upload(self, file: UploadFile, name: str | None, edition: str | None) -> tuple[StandardUploadResponse, bool]:
        pdf = await read_pdf_upload(file, self.settings.max_upload_bytes)
        std, created, warnings = await self.service.register(pdf.data, pdf.filename, name or None, edition or None)
        return await self._response(std, not created, warnings), created

    async def list_all(self, limit: int, offset: int, family: str | None) -> list[StandardOut]:
        return [StandardOut.model_validate(s) for s in await self.service.list_all(limit, offset, family)]

    async def tree(self) -> list[StandardFamilyOut]:
        groups: "OrderedDict[str, StandardFamilyOut]" = OrderedDict()
        for s in await self.service.list_all(limit=10_000):
            g = groups.setdefault(s.family, StandardFamilyOut(family=s.family, part1=[], part2=[], other=[]))
            bucket = {"part1": g.part1, "part2": g.part2}.get(s.part_type, g.other)
            bucket.append(StandardOut.model_validate(s))
        return list(groups.values())

    async def get(self, standard_id: str) -> StandardUploadResponse:
        return await self._response(await self.service.get(standard_id), False, [])
