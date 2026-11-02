"""Structured evaluation result (generic: any product, any standard body).

The AI fills `EvaluationAI` (strict JSON schema). The clause-by-clause table is built
deterministically from the TRF and enriched with the AI's clause notes.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FinalVerdict = Literal[
    "REJECT — INSUFFICIENT TECHNICAL EVIDENCE",
    "REPORT ACCEPTED — PRODUCT COMPLIANCE DEMONSTRATED",
    "REPORT ACCEPTED SUBJECT TO MINOR CORRECTIONS",
    "REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD",
    "REPORT REJECTED — NON-CONFORMITY ESTABLISHED",
]
Severity = Literal["Critical", "High", "Medium", "Low", "Not stated"]
GapType = Literal["PRODUCT_IDENTITY_CONFLICT", "NUMERIC_MISMATCH", "LIMIT_EXCEEDED", "INCORRECT_TEST_CONDITION",
                  "MISSING_EVIDENCE", "CLAUSE_NOT_ADDRESSED", "UNJUSTIFIED_NOT_APPLICABLE",
                  "STANDARD_VERSION_MISMATCH", "DOCUMENT_INTEGRITY", "OTHER", "NOT_STATED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Strict):
    clause: str = Field(description="Clause number(s), e.g. '7.1' or '10.1; 10.2'; '—' if general")
    gap_type: GapType
    finding: str = Field(description="Finding reason: what the report shows (values, units) and why it does not "
                                     "meet the requirement")
    reason: str = Field(description="Normative requirement: what the supplied standard requires, one sentence")
    required_action: str = Field(description="One imperative sentence")
    pages: str = Field(description="Report PDF pages, e.g. 'PDF pp.14,79'")
    severity: Severity = Field(description="Critical: blocks any compliance decision or a measured failure; "
                                           "High: evidence missing or wrong for an applicable safety clause; "
                                           "Medium: incomplete or inconsistent record; Low: editorial")
    report_quote: str = Field(description="Verbatim words (max 25) copied from the REPORT that show the finding; "
                                          "'none' if the finding is an absence")
    standard_quote: str = Field(description="Verbatim words (max 25) copied from the supplied STANDARD stating the "
                                            "requirement, or 'not in supplied standards'")
    code_verified: bool = Field(description="Always false; set by code for its own deterministic checks")
    report_check: str = Field(description="Always 'not checked'; filled by code")
    standard_check: str = Field(description="Always 'not checked'; filled by code")


class ClauseNote(BaseModel):
    clause: str
    critical: bool
    note: str


class EvaluationAI(_Strict):
    title_product: str = Field(description="Product type and brand, e.g. 'INGCO SUBMERSIBLE PUMP'")
    report_number: str
    laboratory: str
    report_date: str
    product_declared: str = Field(description="Brand, model, ratings, class, IP as declared on cover/nameplate")
    declared_model: str
    standards_claimed: str = Field(description="Standards and editions the report claims, with pages if they differ")
    standards_evaluated: str = Field(description="Supplied standards used, and what could not be verified")
    final_verdict: FinalVerdict
    reason: str = Field(description="One or two sentences behind the verdict")
    recommended_action: str = Field(description="One sentence: what must happen before certification can proceed")
    conclusion: str = Field(description="One paragraph")
    tested_model_matches_declared: bool = Field(description="False if the tested sample/model/rating differs from the declared product")
    findings: list[Finding]


class Gap(BaseModel):
    """Clause of a supplied standard (read by code) that the report does not cover or reports N/A."""
    part: str                      # "Part 1" | "Part 2"
    clause: str
    status: str = ""               # "Not in report" | "Reported N/A"
    requirement: str               # first words of the clause in the standard
    reason: str                    # why it is a gap
    required_action: str
    pages: str


class ClauseReviewRow(BaseModel):
    clause: str
    topic: str
    report_verdict: str
    assessment: str
    required_verification: str
    pdf_page: str
    evidence_status: str


class EvaluationDocument(BaseModel):
    """Everything the renderers need (AI part + deterministic part + metadata)."""

    evaluation_id: str
    created_at: datetime
    report_filename: str
    report_pages: int
    standards: list[str]                 # ["IEC 60335-1:2010 (Part 1)", ...]
    part2_label: str                     # "Part 2-41"
    ai: EvaluationAI
    gaps: list[Gap] = []                 # not filled any more: gaps are AI findings (kept for old results)
    clause_review: list[ClauseReviewRow] = []   # not filled any more (kept for old results)
    model: str | None = None


# ---------- API responses ----------
class EvaluationOut(BaseModel):
    evaluation_id: str
    status: str
    from_cache: bool
    report_id: str
    report_filename: str
    standards_used: list[str]
    final_verdict: str | None
    counts: dict[str, int]
    pdf_path: str | None
    xlsx_path: str | None
    download_pdf: str
    download_xlsx: str
    download_json: str = ""
    warnings: list[str] = []