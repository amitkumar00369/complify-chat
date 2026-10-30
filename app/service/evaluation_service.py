"""Formal evaluation pipeline (golden-workbook output, rendered to Excel + branded PDF).

1. resolve report + standards (Part 2 brings its Part 1)      deterministic
2. extract the TRF clause register from the report PDF       deterministic
3. one structured OpenAI call -> EvaluationAI                 AI (inside)
4. merge clause notes into the clause register                deterministic
5. render .xlsx + .pdf -> reports/{id}/outputs/evaluation_{eval_id}/
"""
import asyncio
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models import ChatSession, Evaluation, Report, ReportFile, Standard
from app.models.base import new_id
from app.schema.evaluation import EvaluationAI, EvaluationDocument
from app.service.ai_service import AIService
from app.service.clause_review import build_clause_review
from app.service.render.excel_renderer import render_xlsx
from app.service.render.pdf_renderer import render_pdf
from app.service.report_service import ReportService
from app.service.standard_service import StandardService
from app.service.storage_service import StorageBackend
from app.service.objective_checks import analyse_report_pdf, apply_objective_checks, checks_as_prompt
from app.service.trf_parser import ClauseRow, parse_clause_rows
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.utils.pdf_utils import UploadedPdf, format_ref, parse_ref, safe_filename
from app.utils.text_utils import standards_key

logger = logging.getLogger(__name__)

PART_LABEL = {"part1": "Part 1", "part2": "Part 2", "part": "Part", "standalone": "Standard"}


class EvaluationService:
    def __init__(self, db: AsyncSession, storage: StorageBackend, ai: AIService):
        self.db, self.storage, self.ai = db, storage, ai
        self.settings = get_settings()
        self.standards = StandardService(db, storage)
        self.reports = ReportService(db, storage)

    # ------------------------------------------------------------------ run
    async def run(self, *, user_id: str, report: UploadedPdf | None, report_id: str | None,
            chat_id: str | None, standard_files: list[UploadedPdf], standard_ids: list[str],
            use_cache: bool, analysis_id: str | None = None) -> tuple[Evaluation, bool, list[str]]:
        warnings: list[str] = []
        chat = await self._chat(chat_id, user_id) if chat_id else None

        # cheap mode: build the workbook from an existing /chat answer (no PDFs sent again)
        analysis: Report | None = None
        if analysis_id:
            analysis = await self.db.get(Report, analysis_id)
            if not analysis:
                raise NotFoundError(f"Analysis '{analysis_id}' not found (use analysis_id from /chat)")
            if analysis.user_id != user_id:
                raise ForbiddenError("This analysis belongs to another user")
            report_id = report_id or analysis.report_file_id
            standard_ids = standard_ids or list(analysis.standard_ids)

        # 1) report
        if report:
            rf = await self.reports.register_file(report.data, report.filename)
        elif report_id:
            rf = await self.db.get(ReportFile, report_id)
            if not rf:
                raise NotFoundError(f"Report '{report_id}' not found")
        elif chat and chat.active_report_file_id:
            rf = await self.db.get(ReportFile, chat.active_report_file_id)
        else:
            raise BadRequestError("Send report_file, report_id, or a chat_id that has a report")

        # 2) standards
        requested: list[Standard] = []
        for f in standard_files:
            std, _, w = await self.standards.register(f.data, f.filename)
            requested.append(std)
            warnings += w
        requested += await self.standards.get_many(standard_ids)
        selected = await self.standards.with_parents(requested)
        if not selected and rf.detected_standards:
            selected = await self.standards.pick_for_report([parse_ref(r) for r in rf.detected_standards])
        if not selected and chat and chat.active_standard_ids:
            selected = await self.standards.get_many(chat.active_standard_ids, ignore_missing=True)
        if not selected:
            raise BadRequestError(
                "No standard available for this report. Upload the standards first. Report mentions: "
                + (", ".join(rf.detected_standards) or "none"))
        if len(selected) > self.settings.max_standards_in_context:
            raise BadRequestError(f"Too many standards ({len(selected)}); max {self.settings.max_standards_in_context}")
        for s in selected:
            if s.part_type == "part2" and not s.parent_id:
                warnings.append(f"{s.name}: Part 1 parent not uploaded, evaluated with Part 2 only")

        key = standards_key([s.id for s in selected]) + (f"|from:{analysis.id}" if analysis else "")

        # 3) cache: same report + same standards -> reuse
        if use_cache:
            hit = await self.db.scalar(select(Evaluation).where(
                Evaluation.report_file_id == rf.id, Evaluation.standards_key == key,
                Evaluation.status == "completed").order_by(Evaluation.created_at.desc()))
            if hit:
                return hit, True, warnings

        # 4) deterministic clause register
        texts, facts, checks = await analyse_report_pdf(await self.storage.get(rf.storage_key), rf.sha256)
        clause_rows, pages = parse_clause_rows(texts), len(texts)
        checks_text = checks_as_prompt(facts, checks)
        if not clause_rows:
            warnings.append("No TRF clause table found in the report; clause register is empty.")

        # 5) AI
        file_ids, labels = [], []
        if analysis:                                   # text only: prior review + clause register
            labels = [f"STANDARD {format_ref(s.name, s.edition)}" for s in selected]
            labels.append(f"REPORT: {rf.original_filename} ({pages} PDF pages) - not attached")
            prompt = _build_prompt(labels, selected, clause_rows, prior_review=analysis.output, checks=checks_text)
            warnings.append("Built from the existing chat analysis (PDFs not re-read). "
                            "Run without analysis_id for a full evaluation.")
        else:
            for s in selected:
                file_ids.append(await self.standards.ensure_openai_file(s, self.ai))
                labels.append(f"STANDARD {format_ref(s.name, s.edition)} ({PART_LABEL.get(s.part_type, 'Standard')}"
                              f"{' ' + s.part_number if s.part_type == 'part2' else ''}): {s.original_filename}")
            file_ids.append(await self.reports.ensure_openai_file(rf, self.ai))
            labels.append(f"REPORT: {rf.original_filename} ({pages} PDF pages)")
            prompt = _build_prompt(labels, selected, clause_rows, checks=checks_text)
        logger.info("Evaluation start report=%s standards=%s clauses=%d mode=%s",
                    rf.storage_key, [format_ref(s.name, s.edition) for s in selected], len(clause_rows),
                    "from-analysis" if analysis else "full")
        ai_result, meta = await self.ai.evaluate(prompt, file_ids)
        ai_result = apply_objective_checks(ai_result, facts, checks)   # code wins over AI text

        # 6) build + render + store
        eval_id = new_id()
        doc = self._document(eval_id, rf, pages, selected, ai_result, clause_rows, meta.model)
        pdf_key, xlsx_key = await self._render_and_store(doc, rf)
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
        await self.db.commit()
        return row, False, warnings

    # ------------------------------------------------------ no-LLM from markdown
    async def run_from_markdown(self, *, user_id: str, analysis_id: str | None, report_id: str | None,
                          markdown: str | None) -> tuple[Evaluation, bool, list[str]]:
        """Excel/PDF from a /chat answer (or an uploaded .md) with NO AI call."""
        from app.service.md_evaluation_parser import parse_chat_markdown, report_meta_from_text

        analysis: Report | None = None
        if analysis_id:
            analysis = await self.db.get(Report, analysis_id)
            if not analysis:
                raise NotFoundError(f"Analysis '{analysis_id}' not found (use analysis_id from /chat)")
            if analysis.user_id != user_id:
                raise ForbiddenError("This analysis belongs to another user")
        md = markdown or (analysis.output if analysis else None)
        if not md:
            raise BadRequestError("Send analysis_id, or markdown_file together with report_id")
        rf_id = report_id or (analysis.report_file_id if analysis else None)
        rf = await self.db.get(ReportFile, rf_id) if rf_id else None
        if not rf:
            raise BadRequestError("Report not found: send report_id (report.id from /chat)")
        if not await self.storage.exists(rf.storage_key):
            raise BadRequestError(f"Report PDF missing on disk ({rf.storage_key}); upload it again via /chat")

        selected = await self.standards.get_many(analysis.standard_ids, ignore_missing=True) if analysis else []
        if not selected and rf.detected_standards:
            selected = await self.standards.pick_for_report([parse_ref(r) for r in rf.detected_standards])

        data = await self.storage.get(rf.storage_key)
        texts, facts, checks = await analyse_report_pdf(data, rf.sha256)
        meta = report_meta_from_text("\n".join(texts[:3]))
        meta.setdefault("standards_claimed", ", ".join(rf.detected_standards or []) or "not stated in review")
        ai = apply_objective_checks(parse_chat_markdown(md, meta), facts, checks)
        clause_rows, pages = parse_clause_rows(texts), len(texts)

        eval_id = new_id()
        doc = self._document(eval_id, rf, pages, selected, ai, clause_rows, "no-llm (markdown)")
        pdf_key, xlsx_key = await self._render_and_store(doc, rf)
        row = Evaluation(
            id=eval_id, user_id=user_id, report_file_id=rf.id,
            chat_id=analysis.chat_id if analysis else None,
            standard_ids=[s.id for s in selected], standard_names=[format_ref(s.name, s.edition) for s in selected],
            standards_key=standards_key([s.id for s in selected]) + f"|md:{analysis_id or 'upload'}",
            status="completed", final_verdict=ai.final_verdict, result=doc.model_dump(mode="json"),
            pdf_path=pdf_key, xlsx_path=xlsx_key, model="no-llm (markdown)",
        )
        self.db.add(row)
        await self.db.commit()
        logger.info("Evaluation (no LLM) %s from %s: %d findings, %d clause rows", eval_id,
                    "analysis " + analysis_id if analysis_id else "uploaded markdown",
                    len(ai.critical_findings), len(clause_rows))
        return row, False, ["Built from the chat answer without an AI call; findings are exactly those in the answer."]

    @staticmethod
    async def clone_for(db: AsyncSession, ev: Evaluation, user_id: str, chat_id: str | None) -> Evaluation:
        """Same evaluation for another user/chat: new row, same result + same files on disk."""
        row = Evaluation(
            id=new_id(), user_id=user_id, chat_id=chat_id, report_file_id=ev.report_file_id,
            standard_ids=ev.standard_ids, standard_names=ev.standard_names, standards_key=ev.standards_key,
            status=ev.status, final_verdict=ev.final_verdict, result=ev.result, pdf_path=ev.pdf_path,
            xlsx_path=ev.xlsx_path, model=ev.model, input_tokens=0, output_tokens=0,
        )
        db.add(row)
        await db.commit()
        return row

    # ------------------------------------------------------------ helpers
    async def rerender(self, evaluation_id: str, user_id: str) -> Evaluation:
        """Re-create PDF/XLSX from the stored result (no AI call) - use after changing the format."""
        ev = await self.get(evaluation_id, user_id)
        doc = EvaluationDocument.model_validate(ev.result)
        rf = await self.db.get(ReportFile, ev.report_file_id)
        ev.pdf_path, ev.xlsx_path = await self._render_and_store(doc, rf)
        await self.db.commit()
        return ev

    async def get(self, evaluation_id: str, user_id: str | None = None) -> Evaluation:
        ev = await self.db.get(Evaluation, evaluation_id)
        if not ev:
            raise NotFoundError(f"Evaluation '{evaluation_id}' not found")
        if user_id and ev.user_id != user_id:
            raise ForbiddenError("This evaluation belongs to another user")
        return ev

    async def list_all(self, user_id: str, limit: int = 50) -> "list[Evaluation]":
        stmt = (select(Evaluation).where(Evaluation.user_id == user_id)
                .order_by(Evaluation.created_at.desc()).limit(limit))
        return list(await self.db.scalars(stmt))

    async def _chat(self, chat_id: str, user_id: str) -> ChatSession:
        chat = await self.db.get(ChatSession, chat_id)
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

    async def _render_and_store(self, doc: EvaluationDocument, rf: ReportFile) -> tuple[str, str]:
        base = f"{self.settings.reports_prefix}{rf.id}/outputs/evaluation_{doc.evaluation_id}/"
        name = "Technical_Evaluation_" + safe_filename(doc.ai.report_number or rf.original_filename)
        pdf_key, xlsx_key = f"{base}{name}.pdf", f"{base}{name}.xlsx"
        pdf_bytes, xlsx_bytes = await asyncio.gather(asyncio.to_thread(render_pdf, doc),
                                                     asyncio.to_thread(render_xlsx, doc))
        await self.storage.put(pdf_key, pdf_bytes, content_type="application/pdf")
        await self.storage.put(xlsx_key, xlsx_bytes,
                         content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return pdf_key, xlsx_key


def _build_prompt(labels: list[str], selected: list[Standard], rows: list[ClauseRow],
                  prior_review: str | None = None, checks: str | None = None) -> str:
    lines = ["Files attached (same order):" if not prior_review else "Documents (not attached):"]
    lines += [f"- {l}" for l in labels]
    if any(s.part_type == "part2" for s in selected):
        lines.append("Rule: Part 2 clauses replace, modify or add to the Part 1 clause with the same number; "
                     "where Part 2 is silent, Part 1 applies.")
    lines += ["", "Clause register extracted from the report (clause | report verdict | PDF pages | topic):"]
    for r in rows:
        lines.append(f"{r.clause} | {r.verdict or '-'} | {','.join(map(str, r.pages))} | "
                     f"{re.sub(r'[|]', '/', r.topic)}{(' | result: ' + r.remark) if r.remark else ''}")
    if checks:
        lines += ["", checks]
    if prior_review:
        lines += ["", "PRIOR ENGINEERING REVIEW of this report (source of all findings):", prior_review]
    lines += ["", "Produce the full technical evaluation."]
    return "\n".join(lines)
