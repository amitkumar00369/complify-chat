"""OpenAI wrapper: Conversations API (thread memory) + Responses API + Files."""
import logging
from dataclasses import dataclass
from functools import lru_cache

from app.config.settings import Settings, get_settings
from app.utils.exceptions import AIQuotaError, AIServiceError, ConfigError

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """You are Complify, a senior conformity-assessment engineer reviewing test reports \
for certification. Reports and standards come from any scheme or body (IEC, EN/CENELEC, UL/CSA, ISO, \
GSO, BS, AS/NZS, national deviations, ...) and any product type. Derive everything from the documents \
supplied in this conversation; never assume a product type, a standard structure or a report template.

Documents:
- REPORT: the test report under review (any layout: CB/TRF, test report, inspection report).
- STANDARD(S): the requirements to check against. They may be a single standard, a general part plus \
particular parts (a particular part adds, replaces or modifies clauses of the general part; where it is \
silent the general part applies), amendments, or national deviations. Use the edition supplied.

Rules:
- Evaluate the REPORT only against the supplied STANDARDS. A requirement that is not in a supplied \
standard may be mentioned, but its Standard quote must be "not in supplied standards".
- Every finding must be traceable: give a verbatim Report quote (words copied exactly from the report, \
max 25 words) and a verbatim Standard quote (words copied exactly from the supplied standard, max 25 \
words). Code searches both quotes in the PDFs; a quote that cannot be found marks the finding as unverified. \
Do not paraphrase inside the quote columns. Use "none" for the report quote only when the finding is that \
something is absent from the report.
- Never invent clause numbers, values, pages or verdicts. If evidence is missing, say so.
- If no standard is available, say which standard is needed instead of guessing.
- Clause numbers are those of the supplied standard.
- DETERMINISTIC CHECKS and CLAUSE REGISTERS given in the message are produced by code from the documents: \
treat them as facts; never soften an objective failure.
- A measured value above the limit stated in the report's own table is an objective failure, unless the \
report itself states a different applicable limit for that part.
- Formal engineering register, no first person.

When the user asks for an EVALUATION of a report, answer in EXACTLY this markdown structure \
(the system converts it to JSON/Excel/PDF; do not add other tables, do not rename headings or columns):

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
- One row per gap; no rows for clauses that are correct. A clause of a supplied standard (see any CLAUSE \
REGISTER in the message) that applies to this product but is not in the report, or is marked N/A without a \
valid reason, is a gap too (group closely related clauses in one row). This table is the complete output.
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
- Report quote: words copied exactly from the report that show the gap (max 25), or "none" for an absence.
- Standard quote: words copied exactly from the supplied standard stating the requirement (max 25), \
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

For follow-up questions (not a new evaluation), answer concisely in prose with clause and page references.
"""


class AIFilesMissingError(AIServiceError):
    """OpenAI does not know these file ids (other API key / project, or deleted) -> re-upload and retry."""

    def __init__(self, message: str, file_ids: list[str]):
        super().__init__(message)
        self.file_ids = file_ids


def _ai_error(exc: Exception, what: str) -> Exception:
    """Turn OpenAI errors into clear app errors (no credits -> 402, not a vague 502)."""
    import re

    text = str(exc)
    if re.search(r"were not found|No such File|file .* not found", text, re.I):
        ids = re.findall(r"file-[A-Za-z0-9]+", text)
        if ids:
            return AIFilesMissingError(f"{what}: OpenAI files not found {ids}", ids)
    if "insufficient_quota" in text or "no credits remaining" in text or "credit_balance_exhausted" in text:
        return AIQuotaError("OpenAI credits exhausted. Add credits at "
                            "https://platform.openai.com/settings/organization/billing/ and retry.")
    return AIServiceError(f"{what}: {exc}")


@dataclass
class AIResult:
    text: str
    model: str | None = None
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    truncated: bool = False          # the answer hit max_output_tokens and is cut off


class AIService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not self.settings.openai_api_key:
                raise ConfigError("OPENAI_API_KEY is not set")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                timeout=self.settings.openai_timeout_seconds,
            )
        return self._client

    async def create_conversation(self, metadata: dict[str, str]) -> str:
        try:
            conv = await self.client.conversations.create(
                metadata={k: str(v)[:512] for k, v in metadata.items()}
            )
            return conv.id
        except ConfigError:
            raise
        except Exception as exc:
            raise _ai_error(exc, "Could not create conversation") from exc

    async def upload_file(self, filename: str, data: bytes) -> str:
        try:
            f = await self.client.files.create(
                file=(filename, data, "application/pdf"), purpose="user_data"
            )
            return f.id
        except ConfigError:
            raise
        except Exception as exc:
            raise _ai_error(exc, f"Could not upload '{filename}' to OpenAI") from exc

    async def ask(self, conversation_id: str, text: str, file_ids: list[str]) -> AIResult:
        content: list[dict] = [{"type": "input_file", "file_id": fid} for fid in file_ids]
        content.append({"type": "input_text", "text": text})
        try:
            resp = await self.client.responses.create(
                model=self.settings.openai_model,
                conversation=conversation_id,
                instructions=SYSTEM_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                max_output_tokens=self.settings.openai_max_output_tokens,
            )
        except ConfigError:
            raise
        except Exception as exc:
            raise _ai_error(exc, "OpenAI request failed") from exc

        usage = getattr(resp, "usage", None)
        details = getattr(resp, "incomplete_details", None)
        truncated = getattr(resp, "status", "") == "incomplete" and \
            getattr(details, "reason", "") == "max_output_tokens"
        if truncated:
            logger.warning("AI answer cut off at max_output_tokens=%s (response %s) - raise OPENAI_MAX_OUTPUT_TOKENS",
                           self.settings.openai_max_output_tokens, resp.id)
        return AIResult(
            text=(resp.output_text or "").strip(),
            model=getattr(resp, "model", self.settings.openai_model),
            response_id=resp.id,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            truncated=truncated,
        )

    async def evaluate(self, text: str, file_ids: list[str]):
        """One structured call -> (EvaluationAI, AIResult). Not part of any chat thread."""
        from app.schema.evaluation import EvaluationAI

        content: list[dict] = [{"type": "input_file", "file_id": fid} for fid in file_ids]
        content.append({"type": "input_text", "text": text})
        try:
            resp = await self.client.responses.parse(
                model=self.settings.openai_model,
                instructions=EVALUATION_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                text_format=EvaluationAI,
                max_output_tokens=self.settings.eval_max_output_tokens,
            )
        except ConfigError:
            raise
        except Exception as exc:
            raise _ai_error(exc, "OpenAI evaluation request failed") from exc

        parsed = getattr(resp, "output_parsed", None)
        if parsed is None:
            raise AIServiceError("OpenAI returned no structured evaluation (possibly truncated; "
                                 "raise EVAL_MAX_OUTPUT_TOKENS)")
        usage = getattr(resp, "usage", None)
        meta = AIResult(text="", model=getattr(resp, "model", self.settings.openai_model),
                        response_id=resp.id,
                        input_tokens=getattr(usage, "input_tokens", None),
                        output_tokens=getattr(usage, "output_tokens", None))
        return parsed, meta

    async def append_turn(self, conversation_id: str, user_text: str, assistant_text: str) -> None:
        """Write a cached Q/A into the conversation so later turns still 'remember' it."""
        try:
            await self.client.conversations.items.create(
                conversation_id,
                items=[
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": user_text}]},
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": assistant_text}]},
                ],
            )
        except Exception as exc:  # best effort: the cached answer is already returned
            logger.warning("Could not append cached turn to conversation: %s", exc)


EVALUATION_INSTRUCTIONS = """You are a senior conformity-assessment engineer writing a formal technical \
evaluation of a laboratory test report for a certification body. Reports and standards come from any scheme \
or body (IEC, EN, UL/CSA, ISO, GSO, national deviations, ...) and any product type: derive everything from \
the documents supplied, never from assumptions about a product type, standard structure or report template.

Inputs: either (a) the STANDARD PDFs, the REPORT PDF, a clause list extracted from the report and any clause \
register extracted from the standards; or (b) a PRIOR ENGINEERING REVIEW of that report plus the extracted \
lists (no PDFs). In case (b) use only facts stated in the review or the lists and write "not stated in \
review" for anything missing.

Method:
1. Report identity: number, laboratory, date, declared product/model/ratings, standards and editions \
claimed (note every page where they differ).
2. Traceability: does the tested sample, model, supply and standard edition match the declared product? \
Any mismatch is a Critical finding.
3. Applicability: decide from the standards' scope and the product which clauses apply. If a standard has a \
general part and particular parts, the particular part adds, replaces or modifies the general part.
4. Coverage: an applicable clause that is absent from the report, or marked N/A without a valid reason, is a \
finding (group related clauses).
5. Evidence: compare recorded values against the limits of the supplied standard; state whether they are \
attributable to the declared product.
6. Decide the final verdict from the evidence only.

Writing rules (mandatory):
- Formal engineering register. Short declarative sentences. No first person, no markdown, no emojis.
- Numbers with units and the limit. Pages as report PDF page numbers.
- Never invent values, clauses or pages; write "not stated in report" when absent.
- Do not treat different editions as equivalent; state when an amendment was not supplied.
- findings: problems only. finding = what the report shows; reason = the requirement or limit it fails and \
why; required_action = one imperative sentence; pages = report PDF pages; report_quote = words copied \
exactly from the report (max 25, or "none" for an absence); standard_quote = words copied exactly from the \
supplied standard (max 25, or "not in supplied standards"). Code searches both quotes in the PDFs. \
severity: Critical = blocks any compliance decision or a measured failure; High = evidence missing or wrong \
for an applicable safety clause; Medium = incomplete or inconsistent record; Low = editorial. \
code_verified is always false; report_check and standard_check are always "not checked".
- DETERMINISTIC CHECKS in the message are computed by code and binding: never contradict or soften them.
- final_verdict "REJECT — INSUFFICIENT TECHNICAL EVIDENCE" when any objective failure exists or the report \
cannot support any compliance decision.
- Give as many findings as the evidence requires; do not pad.
"""


@lru_cache
def get_ai_service() -> AIService:
    return AIService(get_settings())