import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import Report, ReportFile, Standard
from app.models.base import new_id, utcnow
from app.service.ai_service import AIResult, AIService
from app.service.storage_service import StorageBackend
from app.utils.pdf_utils import detect_standards, extract_text, format_ref, safe_filename, sha256_hex
from app.utils.text_utils import normalize_prompt, similarity, standards_key

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, db: Session, storage: StorageBackend):
        self.db = db
        self.storage = storage
        self.settings = get_settings()

    # ---------- report files ----------
    def register_file(self, data: bytes, filename: str) -> ReportFile:
        """reports/{id}/{filename}.pdf  (same file again -> existing row, no new copy)."""
        sha = sha256_hex(data)
        existing = self.db.scalar(select(ReportFile).where(ReportFile.sha256 == sha))
        if existing:
            return existing

        rf_id = new_id()
        stem = safe_filename(filename.rsplit(".", 1)[0])
        key = f"{self.settings.reports_prefix}{rf_id}/{stem}.pdf"
        self.storage.put(key, data, metadata={"original_filename": filename})

        detected = [format_ref(d.name, d.edition)
                    for d in detect_standards(extract_text(data, max_pages=5))]
        rf = ReportFile(
            id=rf_id, original_filename=filename, storage_key=key, sha256=sha,
            size_bytes=len(data), detected_standards=detected,
        )
        self.db.add(rf)
        self.db.commit()
        logger.info("Registered report %s at %s, detected standards: %s", filename, key, detected)
        return rf

    def ensure_openai_file(self, rf: ReportFile, ai: AIService) -> str:
        if rf.openai_file_id:
            return rf.openai_file_id
        data = self.storage.get(rf.storage_key)
        rf.openai_file_id = ai.upload_file(rf.original_filename, data)
        self.db.commit()
        return rf.openai_file_id

    # ---------- cache ----------
    def find_cached(self, report_file_id: str, standard_ids: list[str], prompt: str) -> Report | None:
        """Same report + same standards + same (normalised) prompt -> stored output."""
        norm = normalize_prompt(prompt)
        base = select(Report).where(
            Report.report_file_id == report_file_id,
            Report.standards_key == standards_key(standard_ids),
        )
        threshold = self.settings.cache_similarity_threshold

        if threshold >= 1.0:
            hit = self.db.scalar(
                base.where(Report.prompt_normalized == norm).order_by(Report.created_at.desc())
            )
        else:
            candidates = self.db.scalars(base.order_by(Report.created_at.desc()).limit(200))
            best, best_score = None, 0.0
            for c in candidates:
                score = similarity(norm, c.prompt_normalized)
                if score > best_score:
                    best, best_score = c, score
            hit = best if best and best_score >= threshold else None

        if hit:
            hit.cache_hits += 1
            self.db.commit()
        return hit

    def save_analysis(self, *, user_id: str, chat_id: str, rf: ReportFile,
                      standards: list[Standard], prompt: str, result: AIResult) -> Report:
        """DB row + result file next to the report: reports/{rf_id}/outputs/{id}.md"""
        ids = [s.id for s in standards]
        row = Report(
            id=new_id(), user_id=user_id, chat_id=chat_id, report_file_id=rf.id,
            report_name=rf.original_filename,
            standard_ids=ids, standard_names=[format_ref(s.name, s.edition) for s in standards],
            standards_key=standards_key(ids),
            prompt=prompt, prompt_normalized=normalize_prompt(prompt),
            output=result.text, model=result.model,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )
        key = f"{self.settings.reports_prefix}{rf.id}/outputs/{row.id}.md"
        self.storage.put(key, _output_markdown(row, rf).encode("utf-8"),
                         content_type="text/markdown")
        row.output_path = key
        self.db.add(row)
        return row

    def list_all(self, user_id: str, chat_id: str | None = None, limit: int = 50) -> "list[Report]":
        stmt = select(Report).where(Report.user_id == user_id)
        if chat_id:
            stmt = stmt.where(Report.chat_id == chat_id)
        return list(self.db.scalars(stmt.order_by(Report.created_at.desc()).limit(limit)))


def _output_markdown(row: Report, rf: ReportFile) -> str:
    standards = ", ".join(row.standard_names) or "none"
    return (
        f"# Evaluation output\n\n"
        f"- Report: {rf.original_filename}\n"
        f"- Standards: {standards}\n"
        f"- Prompt: {row.prompt}\n"
        f"- Model: {row.model or '-'}\n"
        f"- Created: {utcnow():%Y-%m-%d %H:%M UTC}\n\n"
        f"---\n\n{row.output}\n"
    )
