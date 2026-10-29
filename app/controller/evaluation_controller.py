from collections import Counter

from fastapi import UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.controller.chat_controller import parse_id_list
from app.models import Evaluation, ReportFile
from app.schema.evaluation import EvaluationOut
from app.service.ai_service import AIService
from app.service.evaluation_service import EvaluationService
from app.service.storage_service import StorageBackend
from app.utils.exceptions import BadRequestError, NotFoundError
from app.utils.pdf_utils import read_pdf_upload

MEDIA = {"pdf": "application/pdf",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class EvaluationController:
    def __init__(self, db: Session, storage: StorageBackend, ai: AIService):
        self.db, self.storage = db, storage
        self.service = EvaluationService(db, storage, ai)
        self.settings = get_settings()

    def create(self, *, user_id, report_file: UploadFile | None, report_id, chat_id,
               standard_files: list[UploadFile] | None, standard_ids, use_cache: bool) -> EvaluationOut:
        user_id = (user_id or "").strip()
        if not user_id:
            raise BadRequestError("user_id is required")
        files = [f for f in (standard_files or []) if f is not None and f.filename]
        ids = parse_id_list(standard_ids)
        max_std = self.settings.max_standards_per_request
        if len(files) + len(set(ids)) > max_std:
            raise BadRequestError(f"At most {max_std} standards per request (files + ids)")
        mb = self.settings.max_upload_bytes
        ev, cached, warnings = self.service.run(
            user_id=user_id,
            report=read_pdf_upload(report_file, mb) if report_file and report_file.filename else None,
            report_id=(report_id or "").strip() or None,
            chat_id=(chat_id or "").strip() or None,
            standard_files=[read_pdf_upload(f, mb) for f in files],
            standard_ids=ids, use_cache=use_cache,
        )
        return self.to_out(ev, cached, warnings)

    def to_out(self, ev: Evaluation, cached: bool = False, warnings: list[str] | None = None) -> EvaluationOut:
        rf = self.db.get(ReportFile, ev.report_file_id)
        res = ev.result or {}
        ai = res.get("ai", {})
        counts = {
            "decision_points": len(ai.get("decision_points", [])),
            "critical_findings": len(ai.get("critical_findings", [])),
            "part2_coverage": len(ai.get("part2_coverage", [])),
            "part1_corrections": len(ai.get("part1_corrections", [])),
            "clause_rows": len(res.get("clause_review", [])),
        }
        counts.update({f"clauses_{k}": v for k, v in
                       Counter(r["assessment"].split("—")[0].strip() for r in res.get("clause_review", [])).items()})
        prefix = f"{self.settings.api_prefix}/evaluations/{ev.id}/download"
        return EvaluationOut(
            evaluation_id=ev.id, status=ev.status, from_cache=cached, report_id=ev.report_file_id,
            report_filename=rf.original_filename if rf else "", standards_used=ev.standard_names,
            final_verdict=ev.final_verdict, counts=counts, pdf_path=ev.pdf_path, xlsx_path=ev.xlsx_path,
            download_pdf=f"{prefix}/pdf", download_xlsx=f"{prefix}/xlsx", warnings=warnings or [],
        )

    def get(self, evaluation_id: str, user_id: str | None) -> EvaluationOut:
        return self.to_out(self.service.get(evaluation_id, user_id))

    def list_all(self, user_id: str, limit: int) -> list[EvaluationOut]:
        return [self.to_out(e) for e in self.service.list_all(user_id, limit)]

    def rerender(self, evaluation_id: str, user_id: str) -> EvaluationOut:
        return self.to_out(self.service.rerender(evaluation_id, user_id))

    def download(self, evaluation_id: str, fmt: str, user_id: str | None) -> Response:
        if fmt not in MEDIA:
            raise BadRequestError("format must be pdf or xlsx")
        ev = self.service.get(evaluation_id, user_id)
        key = ev.pdf_path if fmt == "pdf" else ev.xlsx_path
        if not key:
            raise NotFoundError("File not generated")
        data = self.storage.get(key)
        name = key.rsplit("/", 1)[-1]
        disposition = "inline" if fmt == "pdf" else "attachment"
        return Response(data, media_type=MEDIA[fmt],
                        headers={"Content-Disposition": f'{disposition}; filename="{name}"'})
