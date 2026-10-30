"""Clause-by-clause register: deterministic TRF rows + AI clause notes -> fixed assessment vocabulary."""
from app.schema.evaluation import ClauseNote, ClauseReviewRow
from app.service.trf_parser import ClauseRow

A_CRITICAL = "CRITICAL / SPECIFIC REVIEW"
A_PARENT = "REVIEW—PARENT"
A_NA = "REPORTED N/A—APPLICABILITY TO CONFIRM"
A_FAIL = "REPORTED FAIL—NON-CONFORMITY"
A_NONE = "VERDICT NOT STATED—TO CONFIRM"
A_GAP = "NOT IN REPORT—GAP"

EV_UNVERIFIED = "Report extraction; source attribution not independently verified"
EV_ACCEPTED = "Report extraction; consistent with declared product"
EV_REVIEWED = "Report extraction; engineering review note applied"


def build_clause_review(rows: list[ClauseRow], notes: list[ClauseNote],
                        declared_model: str, model_ok: bool) -> list[ClauseReviewRow]:
    note_map = {n.clause.strip(): n for n in notes}
    critical_tops = {n.clause.split(".")[0] for n in notes if n.critical}
    model = (declared_model or "DECLARED MODEL").upper()
    a_pass = "REPORTED PASS—ACCEPTED ON REPORT EVIDENCE" if model_ok else f"REPORTED PASS—NOT VERIFIED TO {model}"

    out: list[ClauseReviewRow] = []
    for r in rows:
        n = note_map.get(r.clause)
        verification = ""
        if n and n.critical:
            assessment, verification = A_CRITICAL, f"CRITICAL: {n.note}"
        elif r.is_parent and r.clause in critical_tops:
            assessment, verification = A_PARENT, "Sub-clause(s) under specific review"
        elif r.verdict == "P":
            assessment = a_pass
        elif r.verdict == "N/A":
            assessment = A_NA
        elif r.verdict == "F":
            assessment = A_FAIL
        else:
            assessment = A_NONE
        if n and not n.critical:
            verification = n.note
        evidence = EV_REVIEWED if n else (EV_ACCEPTED if model_ok else EV_UNVERIFIED)
        out.append(ClauseReviewRow(
            clause=r.clause, topic=r.topic, report_verdict=r.verdict or "—",
            assessment=assessment, required_verification=verification,
            pdf_page=r.page_ref, evidence_status=evidence,
        ))

    # requirements the evaluation flags but the report's clause table does not contain at all
    present = {r.clause for r in rows}
    for n in notes:
        c = n.clause.strip()
        # clauses 1-5 are scope/definitions/general conditions, not testable requirements
        if n.critical and "." in c and c not in present and c.split(".")[0].isdigit() and int(c.split(".")[0]) >= 6:
            out.append(ClauseReviewRow(
                clause=c, topic="Not found in report clause table", report_verdict="—",
                assessment=A_GAP, required_verification=f"MISSING: {n.note}", pdf_page="—",
                evidence_status="Requirement absent from report; evidence/applicability to be supplied",
            ))
    return out
