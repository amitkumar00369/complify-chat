"""Formal evaluation pipeline (golden-workbook output, rendered to Excel + branded PDF).

1. resolve report + standards (Part 2 brings its Part 1)      deterministic
2. extract the TRF clause register from the report PDF       deterministic
3. one structured OpenAI call -> EvaluationAI                 AI (inside)
4. merge clause notes into the clause register                deterministic
5. render .xlsx + .pdf -> reports/{id}/outputs/evaluation_{eval_id}/
"""
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import ChatSession, Evaluation, ReportFile, Standard
from app.models.base import new_id
from app.schema.evaluation import EvaluationAI, EvaluationDocument
from app.service.ai_service import AIService
from app.service.clause_review import build_clause_review
from app.service.render.excel_renderer import render_xlsx
from app.service.render.pdf_renderer import render_pdf
from app.service.report_service import ReportService
from app.service.standard_service import StandardService
from app.service.storage_service import StorageBackend
from app.service.trf_parser import ClauseRow, extract_clause_rows
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.utils.pdf_utils import UploadedPdf, format_ref, parse_ref, safe_filename
from app.utils.text_utils import standards_key

logger = logging.getLogger(__name__)

PART_LABEL = {"part1": "Part 1", "part2": "Part 2", "part": "Part", "standalone": "Standard"}


class EvaluationService:
    def __init__(self, db: Session, storage: StorageBackend, ai: AIService):
        self.db, self.storage, self.ai = db, storage, ai
        self.settings = get_settings()
        self.standards = StandardService(db, storage)
        self.reports = ReportService(db, storage)

    # ------------------------------------------------------------------ run
    def run(self, *, user_id: str, report: UploadedPdf | None, report_id: str | None,
            chat_id: str | None, standard_files: list[UploadedPdf], standard_ids: list[str],
            use_cache: bool) -> tuple[Evaluation, bool, list[str]]:
        warnings: list[str] = []
        chat = self._chat(chat_id, user_id) if chat_id else None

        # 1) report
        if report:
            rf = self.reports.register_file(report.data, report.filename)
        elif report_id:
            rf = self.db.get(ReportFile, report_id)
            if not rf:
                raise NotFoundError(f"Report '{report_id}' not found")
        elif chat and chat.active_report_file_id:
            rf = self.db.get(ReportFile, chat.active_report_file_id)
        else:
            raise BadRequestError("Send report_file, report_id, or a chat_id that has a report")

        # 2) standards
        requested: list[Standard] = []
        for f in standard_files:
            std, _, w = self.standards.register(f.data, f.filename)
            requested.append(std)
            warnings += w
        requested += self.standards.get_many(standard_ids)
        selected = self.standards.with_parents(requested)
        if not selected and rf.detected_standards:
            selected = self.standards.pick_for_report([parse_ref(r) for r in rf.detected_standards])
        if not selected and chat and chat.active_standard_ids:
            selected = self.standards.get_many(chat.active_standard_ids, ignore_missing=True)
        if not selected:
            raise BadRequestError(
                "No standard available for this report. Upload the standards first. Report mentions: "
                + (", ".join(rf.detected_standards) or "none"))
        if len(selected) > self.settings.max_standards_in_context:
            raise BadRequestError(f"Too many standards ({len(selected)}); max {self.settings.max_standards_in_context}")
        for s in selected:
            if s.part_type == "part2" and not s.parent_id:
                warnings.append(f"{s.name}: Part 1 parent not uploaded, evaluated with Part 2 only")

        key = standards_key([s.id for s in selected])

        # 3) cache: same report + same standards -> reuse
        if use_cache:
            hit = self.db.scalar(select(Evaluation).where(
                Evaluation.report_file_id == rf.id, Evaluation.standards_key == key,
                Evaluation.status == "completed").order_by(Evaluation.created_at.desc()))
            if hit:
                return hit, True, warnings

        # 4) deterministic clause register
        clause_rows, pages = extract_clause_rows(self.storage.get(rf.storage_key))
        if not clause_rows:
            warnings.append("No TRF clause table found in the report; clause register is empty.")

        # 5) AI
        file_ids, labels = [], []
        for s in selected:
            file_ids.append(self.standards.ensure_openai_file(s, self.ai))
            labels.append(f"STANDARD {format_ref(s.name, s.edition)} ({PART_LABEL.get(s.part_type, 'Standard')}"
                          f"{' ' + s.part_number if s.part_type == 'part2' else ''}): {s.original_filename}")
        file_ids.append(self.reports.ensure_openai_file(rf, self.ai))
        labels.append(f"REPORT: {rf.original_filename} ({pages} PDF pages)")
        prompt = _build_prompt(labels, selected, clause_rows)
        logger.info("Evaluation start report=%s standards=%s clauses=%d",
                    rf.storage_key, [format_ref(s.name, s.edition) for s in selected], len(clause_rows))
        ai_result, meta = self.ai.evaluate(prompt, file_ids)

        # 6) build + render + store
        eval_id = new_id()
        doc = self._document(eval_id, rf, pages, selected, ai_result, clause_rows, meta.model)
        pdf_key, xlsx_key = self._render_and_store(doc, rf)
        row = Evaluation(
            id=eval_id, user_id=user_id, chat_id=chat.id if chat else None, report_file_id=rf.id,
            standard_ids=[s.id for s in selected], standard_names=[format_ref(s.name, s.edition) for s in selected],
            standards_key=key, status="completed", final_verdict=ai_result.final_verdict,
            result=doc.model_dump(mode="json"), pdf_path=pdf_key, xlsx_path=xlsx_key, model=meta.model,
            input_tokens=meta.input_tokens, output_tokens=meta.output_tokens,
        )
        self.db.add(row)
        if chat:
            chat.active_report_file_id = rf.id
            chat.active_standard_ids = [s.id for s in selected]
        self.db.commit()
        return row, False, warnings

    # ------------------------------------------------------------ helpers
    def rerender(self, evaluation_id: str, user_id: str) -> Evaluation:
        """Re-create PDF/XLSX from the stored result (no AI call) - use after changing the format."""
        ev = self.get(evaluation_id, user_id)
        doc = EvaluationDocument.model_validate(ev.result)
        rf = self.db.get(ReportFile, ev.report_file_id)
        ev.pdf_path, ev.xlsx_path = self._render_and_store(doc, rf)
        self.db.commit()
        return ev

    def get(self, evaluation_id: str, user_id: str | None = None) -> Evaluation:
        ev = self.db.get(Evaluation, evaluation_id)
        if not ev:
            raise NotFoundError(f"Evaluation '{evaluation_id}' not found")
        if user_id and ev.user_id != user_id:
            raise ForbiddenError("This evaluation belongs to another user")
        return ev

    def list_all(self, user_id: str, limit: int = 50) -> "list[Evaluation]":
        stmt = (select(Evaluation).where(Evaluation.user_id == user_id)
                .order_by(Evaluation.created_at.desc()).limit(limit))
        return list(self.db.scalars(stmt))

    def _chat(self, chat_id: str, user_id: str) -> ChatSession:
        chat = self.db.get(ChatSession, chat_id)
        if not chat:
            raise NotFoundError(f"Chat '{chat_id}' not found")
        if chat.user_id != user_id:
            raise ForbiddenError("This chat belongs to another user")
        return chat

    def _document(self, eval_id, rf, pages, selected, ai: EvaluationAI, clause_rows, model) -> EvaluationDocument:
        part2 = next((s for s in selected if s.part_type == "part2"), None)
        return EvaluationDocument(
            evaluation_id=eval_id, created_at=datetime.now(timezone.utc),
            report_filename=rf.original_filename, report_pages=pages,
            standards=[f"{format_ref(s.name, s.edition)} ({PART_LABEL.get(s.part_type, 'Standard')}"
                       f"{' ' + s.part_number if s.part_type in ('part2', 'part') else ''})" for s in selected],
            part2_label=f"Part {part2.part_number}" if part2 else "Standard",
            ai=ai,
            clause_review=build_clause_review(clause_rows, ai.clause_notes, ai.declared_model,
                                              ai.tested_model_matches_declared),
            model=model,
        )

    def _render_and_store(self, doc: EvaluationDocument, rf: ReportFile) -> tuple[str, str]:
        base = f"{self.settings.reports_prefix}{rf.id}/outputs/evaluation_{doc.evaluation_id}/"
        name = "Technical_Evaluation_" + safe_filename(doc.ai.report_number or rf.original_filename)
        pdf_key, xlsx_key = f"{base}{name}.pdf", f"{base}{name}.xlsx"
        self.storage.put(pdf_key, render_pdf(doc), content_type="application/pdf")
        self.storage.put(xlsx_key, render_xlsx(doc),
                         content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return pdf_key, xlsx_key


def _build_prompt(labels: list[str], selected: list[Standard], rows: list[ClauseRow]) -> str:
    lines = ["Files attached (same order):"] + [f"- {l}" for l in labels]
    if any(s.part_type == "part2" for s in selected):
        lines.append("Rule: Part 2 clauses replace, modify or add to the Part 1 clause with the same number; "
                     "where Part 2 is silent, Part 1 applies.")
    lines += ["", "Clause register extracted from the report (clause | report verdict | PDF pages | topic):"]
    for r in rows:
        lines.append(f"{r.clause} | {r.verdict or '-'} | {','.join(map(str, r.pages))} | "
                     f"{re.sub(r'[|]', '/', r.topic)}{(' | result: ' + r.remark) if r.remark else ''}")
    lines += ["", "Produce the full technical evaluation."]
    return "\n".join(lines)
