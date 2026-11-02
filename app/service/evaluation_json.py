"""JSON of an evaluation (chat `result` and GET /evaluations/{id}/json) = the gap JSON:

{ "report_id", "gaps": [ {clause_id, gap_type, priority, normative_requirement, finding_reason,
                          evidence_reference, evidence_confidence, verification_status, required_action} ],
  "final_summary": { final_disposition, evaluation_conclusion, total_gaps, recommended_action } }

Built by GapReportParser from the AI answer (+ the code's quote check); same values as the Excel and PDF.
"""
from app.schema.evaluation import EvaluationDocument
from app.service.gap_report import GapReportParser


def evaluation_json(doc: EvaluationDocument) -> dict:
    return GapReportParser.from_evaluation(doc.ai)