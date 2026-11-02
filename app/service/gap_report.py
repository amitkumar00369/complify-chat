"""Evaluation markdown (.md) -> gap JSON.  Generic: any report, any standard body, no AI call.

    from app.service.gap_report import GapReportParser

    data = GapReportParser().parse_file("outputs/<analysis_id>.md")              # dict
    GapReportParser(report_pdf=rep, standards=[("IEC 60335-1:2010", std)]).to_json_file("a.md", "a.json")

Command line:
    python -m app.service.gap_report answer.md -o answer.json [--report report.pdf] [--standard std.pdf ...]

Output structure
{
  "report_id": "...",
  "gaps": [ { "clause_id", "gap_type", "priority", "normative_requirement", "finding_reason",
              "evidence_reference", "evidence_confidence", "verification_status", "required_action" } ],
  "final_summary": { "final_disposition", "evaluation_conclusion", "total_gaps", "recommended_action" }
}

Where every value comes from
  clause_id, gap_type, priority, normative_requirement, finding_reason, required_action  -> the AI answer
  evidence_reference      -> the AI's report pages ("Report pages 4 and 77")
  evidence_confidence     -> code: were the AI's quotes found in the PDFs?  (HIGH / MEDIUM / LOW / NOT_CHECKED)
  verification_status     -> code, same check (see _status)
  final_disposition       -> the AI verdict as an upper-case code
  evaluation_conclusion, recommended_action -> the AI answer
The parser reads older answer formats too (see md_evaluation_parser); missing values become "NOT_STATED".
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.schema.evaluation import EvaluationAI, Finding
from app.service.md_evaluation_parser import parse_chat_markdown

_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Not stated": 4}


def _ok(check: str) -> bool:
    return check.startswith(("verified", "found", "checked by code"))


def _approx(check: str) -> bool:
    return check.startswith("approximate")


def _upper_code(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (text or "").upper()).strip("_") or "NOT_STATED"


def _evidence_reference(pages: str) -> str:
    nums: list[str] = re.findall(r"\d+(?:\s*[–-]\s*\d+)?", pages or "")
    nums = [re.sub(r"\s", "", n).replace("-", "–") for n in nums]
    if not nums:
        return "Not stated"
    if len(nums) == 1:
        return f"Report page{'s' if '–' in nums[0] else ''} {nums[0]}"
    return f"Report pages {', '.join(nums[:-1])} and {nums[-1]}"


def _nothing_to_check(f: Finding) -> bool:
    """Checks not run, or the AI gave no quotes at all (older answers)."""
    no = ("not checked", "no quote given", "report not available", "standard not available")
    return f.report_check in no and f.standard_check in no


def _confidence(f: Finding) -> str:
    """HIGH: report evidence and requirement both found by code (or a code check); MEDIUM: one of them;
    LOW: neither found; NOT_CHECKED: PDFs not available or no quotes given."""
    if f.code_verified:
        return "HIGH"
    if _nothing_to_check(f):
        return "NOT_CHECKED"
    absence = f.report_check == "no quote given"           # e.g. clause not addressed: nothing to quote
    rep = _ok(f.report_check)
    std = _ok(f.standard_check)
    if rep and std or absence and std:
        return "HIGH" if rep else "MEDIUM"
    if rep or std or _approx(f.report_check) or _approx(f.standard_check):
        return "MEDIUM"
    return "LOW"


def _status(f: Finding) -> str:
    """CONFIRMED_REPORT_DEFECT  report evidence found (or the gap is an absence) and the requirement found
    REPORT_EVIDENCE_CONFIRMED report evidence found, requirement not found in the supplied standards
    REQUIREMENT_CONFIRMED     requirement found, report evidence not found
    REQUIRES_MANUAL_REVIEW    neither found -> a person must check this gap
    NOT_VERIFIED              the PDFs were not available, or the AI gave no quotes to check"""
    if f.code_verified:
        return "CONFIRMED_REPORT_DEFECT"
    if _nothing_to_check(f):
        return "NOT_VERIFIED"
    rep = _ok(f.report_check) or _approx(f.report_check) or f.report_check == "no quote given"
    std = _ok(f.standard_check) or _approx(f.standard_check)
    if rep and std:
        return "CONFIRMED_REPORT_DEFECT"
    if rep and f.report_check != "no quote given":
        return "REPORT_EVIDENCE_CONFIRMED"
    if std:
        return "REQUIREMENT_CONFIRMED"
    return "REQUIRES_MANUAL_REVIEW"


class GapReportParser:
    """Markdown evaluation -> gap JSON. Give the PDFs to have every quote checked (optional)."""

    def __init__(self, report_pdf: bytes | None = None, standards: list[tuple[str, bytes]] | None = None):
        self.report_pdf = report_pdf
        self.standards = standards or []

    # ------------------------------------------------------------ entry points
    def parse(self, markdown: str) -> dict:
        ai = parse_chat_markdown(_strip_file_header(markdown))
        if self.report_pdf or self.standards:
            from app.service.evidence_check import verify_findings

            ai = verify_findings(ai, self.report_pdf, self.standards)
        return self.from_evaluation(ai)

    def parse_file(self, path: str | Path) -> dict:
        return self.parse(Path(path).read_text(encoding="utf-8"))

    def to_json_file(self, md_path: str | Path, out_path: str | Path) -> Path:
        out = Path(out_path)
        out.write_text(json.dumps(self.parse_file(md_path), indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    # ------------------------------------------------------------ builder
    @staticmethod
    def from_evaluation(ai: EvaluationAI) -> dict:
        findings = sorted(ai.findings, key=lambda f: _RANK.get(f.severity, 4))
        gaps = [{
            "clause_id": f.clause,
            "gap_type": f.gap_type,
            "priority": _upper_code(f.severity),
            "normative_requirement": f.reason,
            "finding_reason": f.finding,
            "evidence_reference": _evidence_reference(f.pages),
            "evidence_confidence": _confidence(f),
            "verification_status": _status(f),
            "required_action": f.required_action,
        } for f in findings]
        return {
            "report_id": (ai.report_number or "NOT_STATED").split(",")[0].strip(),
            "gaps": gaps,
            "final_summary": {
                "final_disposition": _upper_code(ai.final_verdict),
                "evaluation_conclusion": ai.conclusion,
                "total_gaps": len(gaps),
                "recommended_action": ai.recommended_action,
            },
        }


def _strip_file_header(md: str) -> str:
    """Saved outputs/<id>.md files start with a small header ('# Evaluation output ... ---')."""
    if md.lstrip().startswith("# Evaluation output") and "\n---" in md:
        return md.split("\n---", 1)[1]
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluation markdown -> gap JSON (no AI call)")
    ap.add_argument("markdown")
    ap.add_argument("-o", "--out", help="output .json (default: next to the .md)")
    ap.add_argument("--report", help="report PDF: check the AI's report quotes")
    ap.add_argument("--standard", nargs="*", default=[], help="standard PDF(s): check the AI's standard quotes")
    a = ap.parse_args()
    parser = GapReportParser(
        report_pdf=Path(a.report).read_bytes() if a.report else None,
        standards=[(Path(p).stem, Path(p).read_bytes()) for p in a.standard])
    out = parser.to_json_file(a.markdown, a.out or Path(a.markdown).with_suffix(".json"))
    print(out)


if __name__ == "__main__":
    main()