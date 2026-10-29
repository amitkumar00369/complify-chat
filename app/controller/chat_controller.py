import json

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.schema.chat import (
    ChatMessageOut, ChatResponse, ChatSessionOut, ReportFileOut, ReportOut, StandardRef, Usage,
)
from app.service.ai_service import AIService
from app.service.analysis_service import AnalysisService, ChatInput
from app.service.chat_service import ChatService
from app.service.report_service import ReportService
from app.service.storage_service import StorageBackend
from app.utils.exceptions import BadRequestError
from app.utils.pdf_utils import parse_ref, read_pdf_upload, standard_name_key


def parse_id_list(raw: str | None) -> list[str]:
    """Accepts 'id1,id2' or '["id1","id2"]'."""
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BadRequestError("standard_ids must be a JSON list or comma-separated") from exc
        if not isinstance(values, list):
            raise BadRequestError("standard_ids must be a list")
        return [str(v).strip() for v in values if str(v).strip()]
    return [p.strip() for p in raw.split(",") if p.strip()]


class ChatController:
    def __init__(self, db: Session, storage: StorageBackend, ai: AIService):
        self.db = db
        self.storage = storage
        self.ai = ai
        self.settings = get_settings()

    def chat(self, *, user_id: str, query: str, chat_id: str | None,
             report_file: UploadFile | None, standard_files: list[UploadFile] | None,
             standard_ids: str | None, use_cache: bool) -> ChatResponse:
        user_id, query = user_id.strip(), query.strip()
        if not user_id:
            raise BadRequestError("user_id is required")
        if not query:
            raise BadRequestError("query is required")

        max_std = self.settings.max_standards_per_request
        files = [f for f in (standard_files or []) if f is not None and f.filename]
        ids = parse_id_list(standard_ids)
        if len(files) > max_std:
            raise BadRequestError(f"At most {max_std} standard PDFs per request")
        if len(files) + len(set(ids)) > max_std:
            raise BadRequestError(f"At most {max_std} standards per request (files + ids)")

        max_bytes = self.settings.max_upload_bytes
        inp = ChatInput(
            user_id=user_id,
            query=query,
            chat_id=(chat_id or "").strip() or None,
            report=read_pdf_upload(report_file, max_bytes) if report_file and report_file.filename else None,
            standard_files=[read_pdf_upload(f, max_bytes) for f in files],
            standard_ids=ids,
            use_cache=use_cache,
        )
        out = AnalysisService(self.db, self.storage, self.ai).handle(inp)

        warnings = list(out.warnings)
        rf = out.report_file
        if rf and rf.detected_standards:
            have = {standard_name_key(s.name) for s in out.standards}
            missing = [r for r in rf.detected_standards if standard_name_key(parse_ref(r)[0]) not in have]
            if missing and not out.standards:
                warnings.append("Report mentions standards that are not uploaded yet: " + ", ".join(missing[:5]))
        if rf is None and not out.standards:
            warnings.append("No report or standard in this chat yet - answered as a general question.")

        r = out.ai_result
        return ChatResponse(
            chat_id=out.chat.id,
            user_id=out.chat.user_id,
            new_chat=out.new_chat,
            user_message_id=out.user_message_id,
            assistant_message_id=out.assistant_message_id,
            answer=out.answer,
            from_cache=out.from_cache,
            analysis_id=out.analysis.id if out.analysis else None,
            output_path=out.analysis.output_path if out.analysis else None,
            report=ReportFileOut(id=rf.id, filename=rf.original_filename,
                                 detected_standards=rf.detected_standards or []) if rf else None,
            standards_used=[StandardRef(id=s.id, name=s.name, edition=s.edition, part_type=s.part_type,
                                        parent_id=s.parent_id) for s in out.standards],
            standards_auto_detected=out.standards_auto_detected,
            warnings=warnings,
            usage=Usage(model=r.model, input_tokens=r.input_tokens, output_tokens=r.output_tokens) if r else None,
        )

    def list_sessions(self, user_id: str, limit: int, offset: int) -> list[ChatSessionOut]:
        return [ChatSessionOut.model_validate(c)
                for c in ChatService(self.db).list_sessions(user_id, limit, offset)]

    def list_messages(self, chat_id: str, user_id: str) -> list[ChatMessageOut]:
        return [ChatMessageOut.model_validate(m)
                for m in ChatService(self.db).list_messages(chat_id, user_id)]

    def list_reports(self, user_id: str, chat_id: str | None, limit: int) -> list[ReportOut]:
        return [ReportOut.model_validate(r)
                for r in ReportService(self.db, self.storage).list_all(user_id, chat_id, limit)]
