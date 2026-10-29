"""OpenAI wrapper: Conversations API (thread memory) + Responses API + Files."""
import logging
from dataclasses import dataclass
from functools import lru_cache

from app.config.settings import Settings, get_settings
from app.utils.exceptions import AIServiceError, ConfigError

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """You are Complify, an expert compliance evaluator for product safety \
test reports (IEC / EN / UL / ISO standards).

Rules:
- Evaluate the REPORT only against the STANDARD documents provided in this conversation.
- For every finding cite the standard clause number AND the report page/section.
- Classify each finding as: Compliant, Non-compliant, Missing evidence, or Not applicable.
- Never invent clause numbers, test values or verdicts. If evidence is missing, say so.
- If no standard is available, say which standard is needed instead of guessing.
- For follow-up questions, use the earlier conversation as context and answer concisely.
"""


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
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.settings.openai_api_key,
                timeout=self.settings.openai_timeout_seconds,
            )
        return self._client

    def create_conversation(self, metadata: dict[str, str]) -> str:
        try:
            conv = self.client.conversations.create(
                metadata={k: str(v)[:512] for k, v in metadata.items()}
            )
            return conv.id
        except ConfigError:
            raise
        except Exception as exc:
            raise AIServiceError(f"Could not create conversation: {exc}") from exc

    def upload_file(self, filename: str, data: bytes) -> str:
        try:
            f = self.client.files.create(
                file=(filename, data, "application/pdf"), purpose="user_data"
            )
            return f.id
        except ConfigError:
            raise
        except Exception as exc:
            raise AIServiceError(f"Could not upload '{filename}' to OpenAI: {exc}") from exc

    def ask(self, conversation_id: str, text: str, file_ids: list[str]) -> AIResult:
        content: list[dict] = [{"type": "input_file", "file_id": fid} for fid in file_ids]
        content.append({"type": "input_text", "text": text})
        try:
            resp = self.client.responses.create(
                model=self.settings.openai_model,
                conversation=conversation_id,
                instructions=SYSTEM_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                max_output_tokens=self.settings.openai_max_output_tokens,
            )
        except ConfigError:
            raise
        except Exception as exc:
            raise AIServiceError(f"OpenAI request failed: {exc}") from exc

        usage = getattr(resp, "usage", None)
        return AIResult(
            text=(resp.output_text or "").strip(),
            model=getattr(resp, "model", self.settings.openai_model),
            response_id=resp.id,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )

    def evaluate(self, text: str, file_ids: list[str]):
        """One structured call -> (EvaluationAI, AIResult). Not part of any chat thread."""
        from app.schema.evaluation import EvaluationAI

        content: list[dict] = [{"type": "input_file", "file_id": fid} for fid in file_ids]
        content.append({"type": "input_text", "text": text})
        try:
            resp = self.client.responses.parse(
                model=self.settings.openai_model,
                instructions=EVALUATION_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                text_format=EvaluationAI,
                max_output_tokens=self.settings.eval_max_output_tokens,
            )
        except ConfigError:
            raise
        except Exception as exc:
            raise AIServiceError(f"OpenAI evaluation request failed: {exc}") from exc

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

    def append_turn(self, conversation_id: str, user_text: str, assistant_text: str) -> None:
        """Write a cached Q/A into the conversation so later turns still 'remember' it."""
        try:
            self.client.conversations.items.create(
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

Inputs: the STANDARD PDFs (Part 1 general requirements and, if present, the Part 2 particular \
requirements that modify Part 1), the REPORT PDF, and a clause register extracted from the report.

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
- Typical sizes: 3–6 decision_points, 15–30 critical_findings, 12–25 part2_coverage rows, \
5–10 part1_corrections, 15–40 clause_notes.
"""


@lru_cache
def get_ai_service() -> AIService:
    return AIService(get_settings())
