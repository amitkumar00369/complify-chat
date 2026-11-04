"""Evaluation from the compliance engine's JSON (no PDFs): effective_kb.json + report_evidence.json.

    effective_kb.json + report_evidence.json
        -> engine_context: compact text (standard clauses with Part 2 changes + report rows with pages)
        -> OpenAI Responses API, one call, no conversation, no file upload
        -> markdown answer (same format as /chat)  -> md_evaluation_parser -> EvaluationAI
        -> code checks: objective checks (IEC 60335-1 rules, only for that series) + quote check
        -> SAME EvaluationDocument -> SAME render_pdf / render_xlsx / evaluation_json as /evaluations

Files: reports/<report name>/outputs/engine_evaluation_<id>/Technical_Evaluation_<report no>.pdf|.xlsx|.json|.md
Index: reports/_engine_index/<id>.json (file keys) and reports/_engine_index/sha_<hash>.json (cache)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from app.config.settings import get_settings
from app.schema.evaluation import EvaluationAI, EvaluationDocument
from app.service.ai_service import AIService, _ai_error
from app.service.engine_context import BuiltContext, build_context
from app.service.evaluation_json import evaluation_json
from app.service.md_evaluation_parser import parse_chat_markdown, report_meta_from_text
from app.service.render.excel_renderer import render_xlsx
from app.service.render.pdf_renderer import render_pdf
from app.service.storage_service import StorageBackend
from app.utils.exceptions import AIQuotaError, AIServiceError, AppError, BadRequestError, ConfigError, NotFoundError
from app.utils.pdf_utils import safe_filename
from app.models.base import new_id

logger = logging.getLogger(__name__)


class EvaluationUnavailableError(AppError):
    status_code = 503
    code = "evaluation_unavailable"

    def __init__(self):
        super().__init__("The evaluation could not be completed right now. Please try again later.")

MEDIA = {"pdf": "application/pdf", "json": "application/json", "md": "text/markdown; charset=utf-8",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

ENGINE_INSTRUCTIONS = """You are Complify, a senior conformity-assessment engineer reviewing test reports \
for certification. Reports and standards come from any scheme or body (IEC, EN/CENELEC, UL/CSA, ISO, \
GSO, BS, AS/NZS, national deviations, ...) and any product type. Derive everything from the material \
supplied in this message; never assume a product type, a standard structure or a report template.

Material (both built by code from the PDFs):
- STANDARD: the effective requirements to check against, clause by clause. Where a general part (Part 1) \
and a particular part (Part 2) are supplied, each clause shows the merged result: the clause text, then \
lines "+ Part 2 ADDITION / REPLACEMENT / MODIFICATION / NEW_REQUIREMENT: ..." with the Part 2 text, and \
"NOT APPLICABLE (Part 2)" where Part 2 removes a clause. Where Part 2 is silent, Part 1 applies. Annexes, \
tables and definitions follow the clauses.
- REPORT: the test report under review: front matter (cover, ratings, model, laboratory), then every \
report row in report order as "[clause] p.N | verdict X" followed by the row text. p.N is the report PDF page.

Rules:
- Evaluate the REPORT only against the supplied STANDARD. A requirement that is not in the supplied \
standard may be mentioned, but its Standard quote must be "not in supplied standards".
- Every finding must be traceable: give a verbatim Report quote (words copied exactly from the REPORT text, \
max 25 words) and a verbatim Standard quote (words copied exactly from the STANDARD text, max 25 words). \
Code searches both quotes in that text; a quote that cannot be found marks the finding as unverified. \
Do not paraphrase inside the quote columns. Use "none" for the report quote only when the finding is that \
something is absent from the report.
- Never invent clause numbers, values, pages or verdicts. If evidence is missing, say so.
- Clause numbers are those of the supplied standard. Pages are the report pages shown in the REPORT text.
- The STANDARD lists every clause: a clause that applies to this product but has no report row, or is \
marked N/A without a valid reason, is a gap (group closely related clauses in one row).
- DETERMINISTIC CHECKS given in the message are produced by code: treat them as facts; never soften an \
objective failure.
- A measured value above the limit stated in the report's own table or in the STANDARD is an objective \
failure, unless the report itself states a different applicable limit for that part.
- Formal engineering register, no first person.

Answer in EXACTLY this markdown structure (the system converts it to JSON/Excel/PDF; do not add other \
tables, do not rename headings or columns):

## Report identity
| Field | Value |
|---|---|
| Report number | ... |
| Laboratory | ... |
| Report date | ... |
| Product declared | ... |
| Declared model | ... |
| Standards claimed | ... |
| Standards evaluated | ... |

## Findings
| Clause | Gap type | Priority | Normative requirement | Finding reason | Required action | Pages | Report quote | Standard quote |
|---|---|---|---|---|---|---|---|---|
- One row per gap; no rows for clauses that are correct. This table is the complete output.
- Clause: number(s), e.g. "7.1" or "10.1; 10.2".
- Gap type: exactly one of PRODUCT_IDENTITY_CONFLICT (declared vs tested product, model or rating differ), \
NUMERIC_MISMATCH (values or calculations inconsistent), LIMIT_EXCEEDED (measured value above the limit), \
INCORRECT_TEST_CONDITION (wrong voltage, load, temperature, duration, method), MISSING_EVIDENCE (result \
claimed without data), CLAUSE_NOT_ADDRESSED (applicable clause absent from the report), \
UNJUSTIFIED_NOT_APPLICABLE (N/A without valid reason), STANDARD_VERSION_MISMATCH (editions/amendments), \
DOCUMENT_INTEGRITY (pages, numbering, attribution), OTHER.
- Priority: CRITICAL (blocks any compliance decision, or a measured failure), HIGH (evidence missing or \
wrong for an applicable safety clause), MEDIUM (incomplete or inconsistent record), LOW (editorial).
- Normative requirement: what the supplied standard requires for this clause, in one sentence.
- Finding reason: what the report shows (values and units) and why it does not meet the requirement.
- Required action: one imperative sentence.
- Pages: report PDF page numbers, e.g. "77, 79".
- Report quote: words copied exactly from the REPORT text that show the gap (max 25), or "none" for an absence.
- Standard quote: words copied exactly from the STANDARD text stating the requirement (max 25), \
or "not in supplied standards".

## Final decision
**Verdict:** exactly one of: REJECT — INSUFFICIENT TECHNICAL EVIDENCE | \
REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD | \
REPORT REJECTED — NON-CONFORMITY ESTABLISHED | REPORT ACCEPTED SUBJECT TO MINOR CORRECTIONS | \
REPORT ACCEPTED — PRODUCT COMPLIANCE DEMONSTRATED
**Reason:** one or two sentences.
**Recommended action:** one sentence: what must happen before certification can proceed.

## Overall conclusion
One paragraph.
"""

TASK = ("Evaluate the REPORT against the STANDARD above. Check every applicable clause of the STANDARD, "
        "including Part 2 additions, new clauses and annexes, against the report rows and appended tables. "
        "Answer in the required markdown structure only.")


def _page_list(pages: dict[int, str]) -> list[str]:
    """{page: text} -> [text of page 1, page 2, ...] (missing pages empty)."""
    if not pages:
        return []
    out = [""] * max(pages)
    for p, t in pages.items():
        if p >= 1:
            out[p - 1] = t
    return out


def _standards_labels(kb: dict) -> tuple[list[str], str]:
    """Same wording as EvaluationService._document: 'IEC 60335-1:2010 (Part 1)', '... (Part 2 2-7)'."""
    base, delta = kb.get("base_standard") or {}, kb.get("delta_standard") or {}

    def ref(m):
        return f"{m.get('standard_number') or '?'}{':' + str(m['year']) if m.get('year') else ''}"
    if not delta:
        return ([f"{ref(base)} (Standard)"] if base else []), "Standard"
    pn = delta.get("part_number") or ""
    return [f"{ref(base)} (Part 1)", f"{ref(delta)} (Part 2 {pn})".rstrip()], (f"Part {pn}" if pn else "Part 2")


class EngineEvaluationService:
    def __init__(self, storage: StorageBackend, ai: AIService):
        self.storage = storage
        self.ai = ai
        self.settings = get_settings()

    # ---------------------------------------------------------------- main
    async def evaluate(self, kb_bytes: bytes, ev_bytes: bytes, *, use_cache: bool = True) -> dict:
        kb, ev = _load(kb_bytes, "effective_kb"), _load(ev_bytes, "report_evidence")
        if not isinstance(kb.get("clauses"), list):
            raise BadRequestError("effective_kb file has no 'clauses' list (send standards/effective_kb.json)")
        if not isinstance(ev.get("clauses"), list):
            raise BadRequestError("report_evidence file has no 'clauses' list (send report/report_evidence.json)")

        sha = hashlib.sha256(kb_bytes + b"\0" + ev_bytes).hexdigest()
        if use_cache:
            cached = await self._read_index(f"sha_{sha}")
            if cached:
                logger.info("Engine evaluation cache hit %s", cached["evaluation_id"])
                return await self._response(cached["evaluation_id"], from_cache=True)

        ctx = build_context(kb, ev)
        logger.info("Engine evaluation: report %s | standards %s | ~%s input tokens",
                    ctx.report_number, ", ".join(ctx.standard_labels), ctx.est_tokens)

        try:
            result = await self._call_ai(ctx)
            md = result["text"]
            if "## Findings" not in md:
                raise AIServiceError("answer is not in the expected markdown format")
        except (AIServiceError, AIQuotaError, ConfigError) as exc:
            # real reason only in the server log; the client sees a neutral message
            logger.error("Engine evaluation failed for report %s: %s", ctx.report_number, exc)
            raise EvaluationUnavailableError() from exc
        ai = parse_chat_markdown(md, report_meta_from_text(ctx.report_text[:6000]))
        report_pages = _page_list(ctx.report_pages)
        ai = await asyncio.to_thread(self._code_checks, ai, report_pages)
        ai = await asyncio.to_thread(self._verify, ai, ctx, report_pages)

        eval_id = new_id()
        labels, part2_label = _standards_labels(kb)
        doc = EvaluationDocument(           # model name is kept only in the server log, never in any output
            evaluation_id=eval_id, created_at=datetime.now(timezone.utc),
            report_filename=ev.get("file_name") or f"{ctx.report_number}.pdf",
            report_pages=max(len(report_pages), _last_page(ev)), standards=labels, part2_label=part2_label,
            ai=ai, model=None,
        )
        keys = await self._store(doc, md, ev)
        logger.info("Engine evaluation %s done: model=%s input=%s cached=%s output=%s tokens, %ss", eval_id,
                    result["model"], result["input_tokens"], result["cached_input_tokens"],
                    result["output_tokens"], result["seconds"])
        meta = {"evaluation_id": eval_id, "sha256": sha, "keys": keys, "document": doc.model_dump(mode="json")}
        await self._write_index(eval_id, meta)
        await self._write_index(f"sha_{sha}", {"evaluation_id": eval_id})
        return await self._response(eval_id, from_cache=False)

    # ---------------------------------------------------------------- AI
    async def _call_ai(self, ctx: BuiltContext) -> dict:
        t0 = time.time()
        try:
            resp = await self.ai.client.responses.create(
                model=self.settings.openai_model,
                instructions=ENGINE_INSTRUCTIONS,
                input=[{"role": "user", "content": [            # standard first: OpenAI prompt caching
                    {"type": "input_text", "text": ctx.standard_text},
                    {"type": "input_text", "text": ctx.report_text},
                    {"type": "input_text", "text": TASK},
                ]}],
                max_output_tokens=self.settings.openai_max_output_tokens,
            )
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _ai_error(exc, "OpenAI request failed") from exc
        usage = getattr(resp, "usage", None)
        inc = getattr(resp, "incomplete_details", None)
        if getattr(resp, "status", "") == "incomplete" and getattr(inc, "reason", "") == "max_output_tokens":
            raise AIServiceError("Answer cut off at OPENAI_MAX_OUTPUT_TOKENS; raise it and retry")
        return {
            "text": (getattr(resp, "output_text", "") or "").strip(),
            "model": getattr(resp, "model", self.settings.openai_model),
            "input_tokens": getattr(usage, "input_tokens", None),
            "cached_input_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "seconds": round(time.time() - t0, 1),
        }

    # ---------------------------------------------------------------- code checks
    @staticmethod
    def _code_checks(ai: EvaluationAI, pages: list[str]) -> EvaluationAI:
        """Same IEC 60335-1 objective checks as /chat (only when the report is from that series)."""
        from app.service.objective_checks import applies_to, apply_objective_checks, run_objective_checks

        if not pages or not applies_to(pages):
            return ai
        facts, checks = run_objective_checks(pages)
        return apply_objective_checks(ai, facts, checks)

    @staticmethod
    def _verify(ai: EvaluationAI, ctx: BuiltContext, report_pages: list[str]) -> EvaluationAI:
        """Quote check against the text the model was given (report rows / standard clauses)."""
        from app.service.evidence_check import _norm, _pages, find_quote

        rep = [("", [_norm(t) for t in report_pages])]
        stds = [(lbl, [_norm(t) for t in _page_list(pages)]) for lbl, pages in ctx.standard_pages.items() if pages]
        for f in ai.findings:
            if f.code_verified and not f.report_quote:
                f.report_check = f.report_check if f.report_check not in ("", "not checked") else "checked by code"
                continue
            f.report_check = find_quote(f.report_quote, rep, _pages(f.pages)) if report_pages else "report not available"
            if f.standard_quote.strip().lower().startswith("not in supplied"):
                f.standard_check = "requirement not in supplied standards"
            else:
                f.standard_check = find_quote(f.standard_quote, stds) if stds else "standard not available"
        return ai

    # ---------------------------------------------------------------- files
    async def _store(self, doc: EvaluationDocument, md: str, ev: dict) -> dict[str, str]:
        stem = safe_filename((ev.get("file_name") or doc.ai.report_number or "report").rsplit(".", 1)[0])
        base = f"{self.settings.reports_prefix}{stem}/outputs/engine_evaluation_{doc.evaluation_id}/"
        name = "Technical_Evaluation_" + safe_filename(doc.ai.report_number or stem)
        keys = {fmt: f"{base}{name}.{fmt}" for fmt in ("pdf", "xlsx", "json", "md")}
        pdf_bytes, xlsx_bytes = await asyncio.gather(asyncio.to_thread(render_pdf, doc),
                                                     asyncio.to_thread(render_xlsx, doc))
        await self.storage.put(keys["pdf"], pdf_bytes, content_type=MEDIA["pdf"])
        await self.storage.put(keys["xlsx"], xlsx_bytes, content_type=MEDIA["xlsx"])
        await self.storage.put(keys["json"], json.dumps(evaluation_json(doc), indent=2, ensure_ascii=False).encode(),
                               content_type=MEDIA["json"])
        header = (f"# Evaluation output\n\n- Report: {doc.report_filename}\n- Standards: {', '.join(doc.standards)}\n"
                  f"- Created: {doc.created_at:%Y-%m-%d %H:%M} UTC\n\n---\n\n")
        await self.storage.put(keys["md"], (header + md).encode(), content_type=MEDIA["md"])
        return keys

    def _index_key(self, name: str) -> str:
        return f"{self.settings.reports_prefix}_engine_index/{name}.json"

    async def _write_index(self, name: str, data: dict) -> None:
        await self.storage.put(self._index_key(name), json.dumps(data).encode(), content_type=MEDIA["json"])

    async def _read_index(self, name: str) -> dict | None:
        key = self._index_key(name)
        if not await self.storage.exists(key):
            return None
        return json.loads(await self.storage.get(key))

    async def _meta(self, evaluation_id: str) -> dict:
        meta = await self._read_index(safe_filename(evaluation_id))
        if not meta:
            raise NotFoundError(f"Engine evaluation '{evaluation_id}' not found")
        return meta

    async def _response(self, evaluation_id: str, *, from_cache: bool) -> dict:
        meta = await self._meta(evaluation_id)
        doc = EvaluationDocument.model_validate(meta["document"])
        base = f"{self.settings.api_prefix}/engine-evaluations/{evaluation_id}"
        return {
            "evaluation_id": evaluation_id,
            "from_cache": from_cache,
            "report_filename": doc.report_filename,
            "standards_used": doc.standards,
            "final_verdict": doc.ai.final_verdict,
            "result": evaluation_json(doc),
            "download_pdf": f"{base}/download/pdf",
            "download_json": f"{base}/download/json",
            "download_md": f"{base}/download/md",
            "download_xlsx": f"{base}/download/xlsx",
        }

    async def get(self, evaluation_id: str) -> dict:
        return await self._response(evaluation_id, from_cache=True)

    async def file(self, evaluation_id: str, fmt: str) -> tuple[bytes, str, str]:
        if fmt not in MEDIA:
            raise BadRequestError("format must be pdf, json, md or xlsx")
        key = (await self._meta(evaluation_id))["keys"][fmt]
        name = key.rsplit("/", 1)[-1].rsplit(".", 1)[0] + f"_{evaluation_id[:8].upper()}.{fmt}"
        return await self.storage.get(key), name, MEDIA[fmt]


def _last_page(ev: dict) -> int:
    def walk(nodes):
        for n in nodes or []:
            yield n
            yield from walk(n.get("children"))
    ends = [e.get("page_end") or 0 for n in walk(ev.get("clauses")) for e in n.get("evidence_items") or []]
    ends += [s.get("page_end") or 0 for s in ev.get("unmapped_sections") or []]
    return max(ends, default=0)


def _load(data: bytes, what: str) -> dict:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise BadRequestError(f"{what} must be a JSON object")
    return obj