"""Structured evaluation result (same shape as the client's golden evaluation workbook).

The AI fills `EvaluationAI` (strict JSON schema). The clause-by-clause table is built
deterministically from the TRF and enriched with the AI's clause notes.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FinalVerdict = Literal[
    "REPORT ACCEPTED — PRODUCT COMPLIANCE DEMONSTRATED",
    "REPORT ACCEPTED SUBJECT TO MINOR CORRECTIONS",
    "REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD",
    "REPORT REJECTED — NON-CONFORMITY ESTABLISHED",
]
Priority = Literal["P0", "P1", "P2"]
RiskLevel = Literal["Critical", "High", "Medium", "Low"]
RequiredVerdict = Literal[
    "PASS", "PASS WITH EVIDENCE", "PASS WITH VERIFIED DETAILS", "PASS WITH TRACEABILITY",
    "PASS SUBJECT TO EVIDENCE", "PASS FOR RECORDED SETUP ONLY", "PASS OR JUSTIFIED N/A",
    "JUSTIFIED N/A IF APPLICABLE", "CONDITIONAL PASS", "CONDITIONAL PASS / HOLD",
    "INSUFFICIENT DETAIL", "REVIEW", "CORRECT", "HOLD / CORRECT", "FAIL / NON-CONFORMITY",
    "CERTIFICATION HOLD",
]
FinalStatus = Literal[
    "ACCEPTED", "PASS WITH EVIDENCE", "PASS OR JUSTIFIED N/A", "CONDITIONAL PASS ONLY",
    "COVERAGE NOT VERIFIED", "REPORT HOLD", "NON-CONFORMITY", "CERTIFICATION HOLD",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionPoint(_Strict):
    priority: Priority
    title: str = Field(description="Short title, e.g. 'Sample identity and traceability'")
    finding: str = Field(description="What the report states, with pages and values")
    required_resolution: str


class CriticalFinding(_Strict):
    clause_or_concern: str = Field(description="e.g. '10.1 Power input' or 'Identity: declared versus tested'")
    report_condition: str = Field(description="What the report records, with numbers and units")
    engineer_comments: str
    required_verdict: RequiredVerdict
    required_action: str
    report_reference: str = Field(description="e.g. 'PDF pp.14–15,79'")


class CoverageRow(_Strict):
    clauses: str = Field(description="Part 2 clause(s), e.g. '15.1.1 / 15.1.2'")
    safety_area: str
    report_status: str
    applicability_rule: str
    required_outcome: str
    risk_level: RiskLevel


class Part1Correction(_Strict):
    clause_or_issue: str
    finding: str
    engineer_comments: str
    required_correction: str
    final_status: FinalStatus


class ClauseNote(_Strict):
    clause: str = Field(description="Clause number exactly as in the report, e.g. '7.1'")
    critical: bool = Field(description="True if this clause needs specific review")
    note: str = Field(description="Short discrepancy / verification note with values, e.g. '96V vs cover 220–240V'")


class EvaluationAI(_Strict):
    title_product: str = Field(description="e.g. 'INGCO SUBMERSIBLE PUMP'")
    report_number: str
    laboratory: str
    report_date: str
    product_declared: str = Field(description="Brand, model, ratings, class, IP as declared on cover/nameplate")
    standards_claimed: str = Field(description="Standards and editions the report claims, with pages if they differ")
    evaluation_basis: str = Field(description="Which supplied standards were used and what could not be verified")
    final_verdict: FinalVerdict
    reason: str
    tested_model_matches_declared: bool = Field(description="False if the tested sample/model/rating differs from the declared product")
    declared_model: str
    decision_points: list[DecisionPoint]
    final_technical_conclusion: str
    critical_findings: list[CriticalFinding]
    part2_coverage: list[CoverageRow]
    part1_corrections: list[Part1Correction]
    clause_notes: list[ClauseNote]


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
    clause_review: list[ClauseReviewRow]
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
    warnings: list[str] = []
