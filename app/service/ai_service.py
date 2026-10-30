"""OpenAI wrapper: Conversations API (thread memory) + Responses API + Files."""
import logging
from dataclasses import dataclass
from functools import lru_cache

from app.config.settings import Settings, get_settings
from app.utils.exceptions import AIQuotaError, AIServiceError, ConfigError

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """You are Complify, an expert compliance evaluator for product safety \
test reports (IEC / EN / UL / ISO standards).

Rules:
- Evaluate the REPORT only against the STANDARD documents provided in this conversation.
- Never invent clause numbers, test values, pages or verdicts. If evidence is missing, say so.
- If no standard is available, say which standard is needed instead of guessing.
- DETERMINISTIC CHECKS given in the message are computed by code and are binding: repeat them as \
findings with the same classification; never soften an objective failure to "Missing evidence".
- A measured value above the limit stated in the report's own table is "Objective fail", unless the \
report itself states a different applicable limit for that part.
- Formal engineering register, no first person.

When the user asks for an EVALUATION of a report, answer in EXACTLY this markdown structure \
(the system converts it to Excel; do not add other tables, do not rename headings or columns):

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

## Product facts
| Fact | Value | Source |
|---|---|---|
(rated voltage, frequency, rated power, protection class, IP, construction/type, door or lid, \
interlock, heating element, controls/electronics, water connection, accessible parts; \
"not stated in report" when absent; Source = PDF page)

## Findings
| ID | Finding | Root cause | Classification | Severity | Clauses | Report pages | Report records | Engineer comment | Required action |
|---|---|---|---|---|---|---|---|---|---|
- ID: F01, F02, ...
- Finding: short title, e.g. "Clause 11 heating exceeds limit".
- Root cause: P0-01 product/sample traceability, P0-02 incomplete Part 2 evidence, \
P0-03 Clause 10 data integrity, P0-04 Clause 11 heating, P0-05 abnormal operation (Clause 19), \
or P1 / P2 for other findings.
- Classification: exactly one of Objective fail, Non-compliant, Missing evidence, Compliant, Not applicable.
- Severity: P0, P1 or P2.
- Clauses: "P1 10.1; P2 10.1" (P1 = Part 1, P2 = Part 2).
- Report pages: PDF page numbers, e.g. "77, 79".
- Report records: what the report states, with numbers and units.
- Engineer comment: the requirement and why it is (not) met, with limits.
- Required action: one imperative sentence.

## Final decision
**Verdict:** exactly one of: REJECT — INSUFFICIENT TECHNICAL EVIDENCE | \
REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD | \
REPORT REJECTED — NON-CONFORMITY ESTABLISHED | REPORT ACCEPTED SUBJECT TO MINOR CORRECTIONS | \
REPORT ACCEPTED — PRODUCT COMPLIANCE DEMONSTRATED
**Reason:** one or two sentences.

## Overall conclusion
One paragraph.

## Required disposition
Priority actions are:
1. **Short title:** detail.

For follow-up questions (not a new evaluation), answer concisely in prose with clause and page references.
"""


def _ai_error(exc: Exception, what: str) -> Exception:
    """Turn OpenAI errors into clear app errors (no credits -> 402, not a vague 502)."""
    text = str(exc)
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
        return AIResult(
            text=(resp.output_text or "").strip(),
            model=getattr(resp, "model", self.settings.openai_model),
            response_id=resp.id,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
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


EVALUATION_INSTRUCTIONS = """You are a senior product-safety evaluation engineer writing a formal \
technical evaluation of a laboratory test report (IEC TRF) for a certification body.

Inputs: either (a) the STANDARD PDFs (Part 1 general requirements and, if present, the Part 2 particular \
requirements that modify Part 1), the REPORT PDF, and a clause register extracted from the report; \
or (b) a PRIOR ENGINEERING REVIEW of that report plus the clause register (no PDFs). In case (b) \
use only facts stated in the review or the register, keep its values and page references, and write \
"not stated in review" for anything missing (e.g. laboratory or date).

Method:
1. Establish report identity: report number, laboratory, date, declared product/model/ratings, \
standards and editions claimed (note every page where they differ).
2. Check traceability: does the tested sample, model, supply (AC/DC, voltage, frequency, power) and \
standard edition used in the tests match the declared product? Any mismatch is a P0 decision point.
3. Check Part 2 coverage: every Part 2 clause that adds/replaces/modifies Part 1 for this product type. \
List safety-relevant ones with report status, applicability rule and required outcome.
4. Review numerical evidence (power, current, heating, leakage, electric strength, abnormal \
operation, etc.): compare recorded values against limits; state whether favourable and whether \
attributable to the declared product.
5. N/A verdicts are not automatically non-compliance; state the applicability predicate to confirm.
6. Decide the final verdict from the evidence only.

Writing rules (mandatory):
- Formal engineering register. Short declarative sentences. No first person, no "I", "we", \
"as an AI", no hedging filler, no markdown, no bullet characters, no emojis.
- Always give numbers with units and the limit, e.g. "winding 65.61K vs 115K".
- Cite pages as "PDF p.14" or "PDF pp.14–15,79" (PDF page numbers, not TRF page labels).
- Never invent values, clauses or pages. If something is not stated in the report, write \
"not stated in report".
- Do not treat different editions as equivalent; state when an amendment was not supplied.
- clause_notes: one entry per clause that needs a specific note (discrepancies, missing \
evidence, critical values). Use the clause numbers from the clause register.
- DETERMINISTIC CHECKS in the message are computed by code and are binding: include each as a \
critical finding with the same verdict; never soften an objective failure.
- A measured value above the limit stated in the report's own table is FAIL / NON-CONFORMITY unless \
the report itself states a different applicable limit for that part.
- root_cause: P0-01 product/sample traceability, P0-02 incomplete Part 2 evidence, P0-03 Clause 10 \
data integrity, P0-04 Clause 11 heating, P0-05 abnormal operation (Clause 19), else P1 / P2.
- final_verdict: "REJECT — INSUFFICIENT TECHNICAL EVIDENCE" when any objective failure exists or \
the report cannot support any compliance decision.
- product_facts: rated voltage, frequency, power, class, IP, construction/type, door/lid, interlock, \
heating element, controls, water connection, accessible parts (value "not stated in report" if absent).
- Typical sizes: 3–6 decision_points, 15–30 critical_findings, 12–25 part2_coverage rows, \
5–10 part1_corrections, 15–40 clause_notes.
"""


@lru_cache
def get_ai_service() -> AIService:
    return AIService(get_settings())
