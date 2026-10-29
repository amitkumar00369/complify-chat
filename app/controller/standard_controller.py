from collections import OrderedDict

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import Standard
from app.schema.standard import StandardFamilyOut, StandardOut, StandardRefOut, StandardUploadResponse
from app.service.standard_service import StandardService
from app.service.storage_service import StorageBackend
from app.utils.pdf_utils import read_pdf_upload


class StandardController:
    def __init__(self, db: Session, storage: StorageBackend):
        self.service = StandardService(db, storage)
        self.settings = get_settings()

    def _response(self, std: Standard, duplicate: bool, warnings: list[str]) -> StandardUploadResponse:
        parent = self.service.parent(std)
        return StandardUploadResponse(
            standard=StandardOut.model_validate(std),
            duplicate=duplicate,
            parent=StandardRefOut.model_validate(parent) if parent else None,
            children=[StandardRefOut.model_validate(c) for c in self.service.children(std)],
            warnings=warnings,
        )

    def upload(self, file: UploadFile, name: str | None, edition: str | None) -> tuple[StandardUploadResponse, bool]:
        pdf = read_pdf_upload(file, self.settings.max_upload_bytes)
        std, created, warnings = self.service.register(pdf.data, pdf.filename, name or None, edition or None)
        return self._response(std, not created, warnings), created

    def list_all(self, limit: int, offset: int, family: str | None) -> list[StandardOut]:
        return [StandardOut.model_validate(s) for s in self.service.list_all(limit, offset, family)]

    def tree(self) -> list[StandardFamilyOut]:
        groups: "OrderedDict[str, StandardFamilyOut]" = OrderedDict()
        for s in self.service.list_all(limit=10_000):
            g = groups.setdefault(s.family, StandardFamilyOut(family=s.family, part1=[], part2=[], other=[]))
            bucket = {"part1": g.part1, "part2": g.part2}.get(s.part_type, g.other)
            bucket.append(StandardOut.model_validate(s))
        return list(groups.values())

    def get(self, standard_id: str) -> StandardUploadResponse:
        return self._response(self.service.get(standard_id), False, [])
