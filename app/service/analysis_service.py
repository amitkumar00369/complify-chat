"""Chat flow: session -> files -> standard resolution -> cache -> OpenAI -> persist."""
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models import ChatMessage, ChatSession, Evaluation, Report, ReportFile, Standard
from app.service.ai_service import AIResult, AIService
from app.service.chat_service import ChatService
from app.service.report_service import ReportService
from app.service.standard_service import StandardService
from app.service.storage_service import StorageBackend
from app.utils.exceptions import BadRequestError
from app.utils.pdf_utils import UploadedPdf, format_ref, parse_ref
from app.utils.text_utils import standards_key

logger = logging.getLogger(__name__)


@dataclass
class ChatInput:
    user_id: str
    query: str
    chat_id: str | None = None
    report: UploadedPdf | None = None
    standard_files: list[UploadedPdf] = field(default_factory=list)
    standard_ids: list[str] = field(default_factory=list)
    use_cache: bool = True
    reuse_report_output: bool = True


@dataclass
class ChatOutcome:
    chat: ChatSession
    new_chat: bool
    answer: str
    from_cache: bool
    user_message_id: str
    assistant_message_id: str
    report_file: ReportFile | None
    standards: list[Standard]
    standards_auto_detected: bool
    analysis: Report | None
    ai_result: AIResult | None
    warnings: list[str] = field(default_factory=list)
    evaluation: Evaluation | None = None        # Excel/PDF built from this answer
    reused_existing_output: bool = False        # same report evaluated before -> no AI call


class AnalysisService:
    def __init__(self, db: AsyncSession, storage: StorageBackend, ai: AIService):
        self.db = db
        self.ai = ai
        self.settings = get_settings()
        self.chats = ChatService(db)
        self.standards = StandardService(db, storage)
        self.reports = ReportService(db, storage)

    async def handle(self, inp: ChatInput) -> ChatOutcome:
        max_std = self.settings.max_standards_per_request
        chat, new_chat = await self.chats.get_or_create(inp.user_id, inp.chat_id, inp.query)

        # 1) standards sent in THIS request (new PDFs and/or ids of already-uploaded ones)
        requested: list[Standard] = []
        warnings: list[str] = []
        for f in inp.standard_files:
            std, _, w = await self.standards.register(f.data, f.filename)
            requested.append(std)
            warnings += w
        requested += await self.standards.get_many(inp.standard_ids)
        requested = _dedupe(requested)
        if len(requested) > max_std:
            raise BadRequestError(f"At most {max_std} standards per request")

        # 2) report: new upload, else the one already active in this chat
        report_file: ReportFile | None = None
        new_report = inp.report is not None
        if inp.report:
            report_file = await self.reports.register_file(inp.report.data, inp.report.filename)
            chat.active_report_file_id = report_file.id
        elif chat.active_report_file_id:
            report_file = await self.db.get(ReportFile, chat.active_report_file_id)

        # 3) which standards apply? (Part 2 always brings its Part 1 parent)
        selected, auto = await self.standards.with_parents(requested), False
        if not selected and new_report and report_file and report_file.detected_standards:
            refs = [parse_ref(r) for r in report_file.detected_standards]
            selected = await self.standards.pick_for_report(refs)
            auto = bool(selected)
        if not selected and chat.active_standard_ids:
            selected = await self.standards.get_many(chat.active_standard_ids, ignore_missing=True)
        if len(selected) > self.settings.max_standards_in_context:
            raise BadRequestError(
                f"Too many standards after adding Part 1 parents ({len(selected)}); "
                f"max {self.settings.max_standards_in_context}"
            )
        for s in selected:
            if s.part_type == "part2" and not s.parent_id:
                warnings.append(f"{s.name}: Part 1 parent not uploaded, evaluating with Part 2 only")
        chat.active_standard_ids = [s.id for s in selected]
        await self.db.commit()
        logger.info(
            "Chat %s | report=%s | detected=%s | using standards: %s",
            chat.id,
            report_file.storage_key if report_file else None,
            report_file.detected_standards if report_file else [],
            [f"{format_ref(s.name, s.edition)} -> {s.storage_key}" for s in selected] or "NONE",
        )

        # 4a) same report uploaded again (any user) -> return its stored evaluation, no AI call
        if (inp.report and report_file and inp.use_cache and inp.reuse_report_output
                and self.settings.reuse_report_output):
            prior = await self._latest_evaluation_answer(report_file.id, chat.active_standard_ids)
            if prior:
                logger.info("Report %s already evaluated (analysis %s) -> reusing output", report_file.id, prior.id)
                out = await self._answer_from_cache(inp, chat, new_chat, report_file, selected, auto, prior)
                out.reused_existing_output = True
                out.warnings = warnings + ["This report was evaluated before; the stored evaluation was returned "
                                           "without an AI call. Send reuse_report_output=false to re-evaluate."]
                out.evaluation = await self._export(prior, inp.user_id, chat, out.warnings)
                return out

        # 4b) cache lookup: same report + standards + same prompt
        use_cache = self.settings.cache_enabled and inp.use_cache and report_file is not None
        if use_cache:
            hit = await self.reports.find_cached(report_file.id, chat.active_standard_ids, inp.query)
            if hit:
                logger.info("Cache HIT report=%s chat=%s", hit.id, chat.id)
                out = await self._answer_from_cache(inp, chat, new_chat, report_file, selected, auto, hit)
                out.warnings = warnings
                out.evaluation = await self._export(hit, inp.user_id, chat, out.warnings)
                return out

        # 5) call OpenAI
        new_thread = not chat.openai_conversation_id
        if not chat.openai_conversation_id:
            chat.openai_conversation_id = await self.ai.create_conversation(
                {"chat_id": chat.id, "user_id": inp.user_id}
            )
            await self.db.commit()

        already = set(chat.attached_openai_file_ids or [])
        new_file_ids: list[str] = []
        labels: list[str] = []
        for std in selected:
            fid = await self.standards.ensure_openai_file(std, self.ai)
            if fid not in already:
                new_file_ids.append(fid)
                role = {"part1": "Part 1 - general requirements",
                        "part2": "Part 2 - particular requirements, modifies Part 1"}.get(std.part_type, "standard")
                labels.append(f"STANDARD {format_ref(std.name, std.edition)} ({role}): {std.original_filename}")
        checks_text = None
        if report_file:
            fid = await self.reports.ensure_openai_file(report_file, self.ai)
            if fid not in already:
                new_file_ids.append(fid)
                labels.append(f"REPORT: {report_file.original_filename}")
                # first time this report enters the thread: give the AI the binding code checks
                try:
                    from app.service.objective_checks import analyse_report_pdf, checks_as_prompt
                    _, facts, checks = await analyse_report_pdf(await self.reports.storage.get(report_file.storage_key),
                                                                report_file.sha256)
                    checks_text = checks_as_prompt(facts, checks)
                except Exception as exc:  # checks must never block the chat
                    logger.warning("Objective checks skipped: %s", exc)

        prompt_text = _build_prompt(inp.query, selected, report_file, labels)
        if new_thread:
            prev = await self._previous_answer(chat)
            if prev:   # chat started from a stored evaluation: give the AI that context once
                prompt_text = "EARLIER EVALUATION IN THIS CHAT:\n" + prev[:30000] + "\n\n" + prompt_text
        if checks_text:
            prompt_text = checks_text + "\n\n" + prompt_text
        result = await self.ai.ask(chat.openai_conversation_id, prompt_text, new_file_ids)

        # 6) persist
        chat.attached_openai_file_ids = list(already) + new_file_ids
        analysis = None
        if report_file:
            analysis = await self.reports.save_analysis(
                user_id=inp.user_id, chat_id=chat.id, rf=report_file,
                standards=selected, prompt=inp.query, result=result,
            )
            await self.db.flush()
        user_msg = await self.chats.add_message(chat, "user", inp.query, attachments=_attachment_names(inp))
        bot_msg = await self.chats.add_message(chat, "assistant", result.text,
                                         report_id=analysis.id if analysis else None)
        await self.db.commit()

        out = ChatOutcome(chat, new_chat, result.text, False, user_msg.id, bot_msg.id,
                          report_file, selected, auto, analysis, result, warnings)
        if analysis:
            out.evaluation = await self._export(analysis, inp.user_id, chat, out.warnings)
        return out

    # ------------------------------------------------------------- export helpers
    async def _latest_evaluation_answer(self, report_file_id: str, standard_ids: list[str]) -> Report | None:
        """Newest stored chat answer for this report + standards that is a full evaluation."""
        stmt = (select(Report).where(Report.report_file_id == report_file_id,
                                     Report.standards_key == standards_key(standard_ids))
                .order_by(Report.created_at.desc()).limit(20))
        for row in await self.db.scalars(stmt):
            if _is_evaluation(row.output):
                return row
        return None

    async def _previous_answer(self, chat: ChatSession) -> str | None:
        msg = await self.db.scalar(select(ChatMessage).where(ChatMessage.chat_id == chat.id,
                                                       ChatMessage.role == "assistant")
                             .order_by(ChatMessage.created_at.desc()))
        return msg.content if msg else None

    async def _export(self, analysis: Report, user_id: str, chat: ChatSession, warnings: list[str]) -> Evaluation | None:
        """Excel + PDF from the answer (no AI). Reuses files already built for the same answer."""
        if not self.settings.auto_export_chat_evaluation or not _is_evaluation(analysis.output):
            return None
        from app.service.evaluation_service import EvaluationService

        try:
            key = analysis.standards_key + f"|md:{analysis.id}"
            ev = await self.db.scalar(select(Evaluation).where(
                Evaluation.report_file_id == analysis.report_file_id, Evaluation.standards_key == key,
                Evaluation.status == "completed").order_by(Evaluation.created_at.desc()))
            if ev is None:
                ev, _, _ = await EvaluationService(self.db, self.reports.storage, self.ai).run_from_markdown(
                    user_id=analysis.user_id, analysis_id=analysis.id, report_id=None, markdown=None)
            if ev.user_id != user_id or ev.chat_id != chat.id:
                ev = await EvaluationService.clone_for(self.db, ev, user_id, chat.id)
            return ev
        except Exception as exc:  # the chat answer must still be returned
            logger.warning("Excel/PDF export skipped for analysis %s: %s", analysis.id, exc)
            warnings.append(f"Excel/PDF could not be generated: {exc}")
            return None

    async def _answer_from_cache(self, inp, chat, new_chat, report_file, selected, auto, hit: Report) -> ChatOutcome:
        user_msg = await self.chats.add_message(chat, "user", inp.query, attachments=_attachment_names(inp))
        bot_msg = await self.chats.add_message(chat, "assistant", hit.output, report_id=hit.id, from_cache=True)
        await self.db.commit()
        # keep the OpenAI thread in sync so follow-ups still have this context
        if chat.openai_conversation_id:
            await self.ai.append_turn(chat.openai_conversation_id, inp.query, hit.output)
        return ChatOutcome(chat, new_chat, hit.output, True, user_msg.id, bot_msg.id,
                           report_file, selected, auto, hit, None)


def _is_evaluation(md: str | None) -> bool:
    """True if the answer is a full evaluation (has a findings table), not a short follow-up reply."""
    if not md or "|" not in md:
        return False
    try:
        from app.service.md_evaluation_parser import parse_chat_markdown

        return len(parse_chat_markdown(md).critical_findings) > 0
    except Exception:
        return False


def _dedupe(stds: list[Standard]) -> list[Standard]:
    seen, out = set(), []
    for s in stds:
        if s.id not in seen:
            seen.add(s.id)
            out.append(s)
    return out


def _attachment_names(inp: ChatInput) -> list[str]:
    names = [f.filename for f in inp.standard_files]
    if inp.report:
        names.append(inp.report.filename)
    return names


def _build_prompt(query: str, standards: list[Standard], report: ReportFile | None,
                  new_labels: list[str]) -> str:
    lines = []
    if new_labels:
        lines.append("Files attached in this message (same order as above):")
        lines += [f"- {label}" for label in new_labels]
    lines.append("Active context:")
    lines.append(f"- Report: {report.original_filename if report else 'none'}")
    if standards:
        lines.append("- Standards: " + ", ".join(format_ref(s.name, s.edition) for s in standards))
        if any(s.part_type == "part2" for s in standards):
            lines.append("- Rule: Part 2 clauses replace/modify/add to Part 1 clauses with the same "
                         "number; where Part 2 is silent, Part 1 applies.")
    else:
        detected = ", ".join(report.detected_standards) if report and report.detected_standards else "none"
        lines.append(f"- Standards: none uploaded (report mentions: {detected})")
    lines.append("")
    lines.append(f"User query: {query}")
    return "\n".join(lines)
