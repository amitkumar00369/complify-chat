"""Evaluation workbook = exactly the AI output: Summary + Findings (same data as the PDF and JSON)."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config.settings import get_settings
from app.schema.evaluation import EvaluationDocument
from app.service.evidence_check import evidence_summary

THIN = Side(style="thin", color="FFC9D3DE")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")

VERDICT_FILL = {
    "HOLD": "FFF8D7DA", "FAIL": "FFF8D7DA", "NON-CONFORMITY": "FFF8D7DA", "CORRECT": "FFFCE5CD",
    "CONDITIONAL": "FFFFF2CC", "INSUFFICIENT": "FFFFF2CC", "REVIEW": "FFFFF2CC",
    "PASS": "FFD9EAD3", "ACCEPTED": "FFD9EAD3",
}
RISK_FILL = {"Critical": "FFF8D7DA", "High": "FFFCE5CD", "Medium": "FFFFF2CC", "Low": "FFD9EAD3"}


def _hex(color: str) -> str:
    return "FF" + color.lstrip("#").upper()


def _verdict_fill(text: str) -> PatternFill | None:
    t = (text or "").upper()
    for key, color in VERDICT_FILL.items():   # order matters: HOLD before PASS
        if key in t:
            return PatternFill("solid", fgColor=color)
    return None


def _table(ws, headers: list[str], rows: list[list], widths: list[int], header_fill: PatternFill,
           color_col: int | None = None, risk_col: int | None = None) -> None:
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = BORDER
    for r in rows:
        ws.append(r)
        i = ws.max_row
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=i, column=c)
            cell.alignment = WRAP
            cell.border = BORDER
        if color_col is not None:
            fill = _verdict_fill(str(r[color_col]))
            if fill:
                ws.cell(row=i, column=color_col + 1).fill = fill
        if risk_col is not None and r[risk_col] in RISK_FILL:
            ws.cell(row=i, column=risk_col + 1).fill = PatternFill("solid", fgColor=RISK_FILL[r[risk_col]])
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"


def _band(ws, text: str, fill: PatternFill) -> None:
    ws.append([])
    ws.append([text])
    r = ws.max_row
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    ws.cell(row=r, column=1).font = Font(bold=True, color="FFFFFFFF")
    ws.cell(row=r, column=1).fill = fill


def render_xlsx(doc: EvaluationDocument) -> bytes:
    s = get_settings()
    brand = _hex(s.brand_color)
    hfill = PatternFill("solid", fgColor=brand)
    ai = doc.ai
    wb = Workbook()

    # ---- Summary ----
    ws = wb.active
    ws.title = "Summary"
    ws.merge_cells("A1:F1")
    ws["A1"] = f"TECHNICAL EVALUATION — {ai.title_product.upper()}"
    ws["A1"].font = Font(size=15, bold=True, color="FFFFFFFF")
    ws["A1"].fill = hfill
    ws.row_dimensions[1].height = 26
    ws.append([])
    label_font = Font(bold=True, color=brand)
    kv = [
        ("Report", f"{ai.report_number} | {ai.laboratory} | {ai.report_date}"),
        ("Product declared", ai.product_declared),
        ("Standards claimed", ai.standards_claimed),
        ("Standards evaluated", ai.standards_evaluated),
        ("Final verdict", ai.final_verdict),
        ("Reason", ai.reason),
        ("Recommended action", ai.recommended_action),
    ]
    for k, v in kv:
        ws.append([k, v])
        r = ws.max_row
        ws.cell(row=r, column=1).font = label_font
        ws.cell(row=r, column=2).alignment = WRAP
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        if k == "Final verdict":
            ws.cell(row=r, column=2).font = Font(bold=True)
            fill = _verdict_fill(v)
            if fill:
                ws.cell(row=r, column=2).fill = fill
    counts = {lvl: sum(f.severity == lvl for f in ai.findings) for lvl in ("Critical", "High", "Medium", "Low")}
    if any(f.severity == "Not stated" for f in ai.findings):
        counts["Severity not stated"] = sum(f.severity == "Not stated" for f in ai.findings)
    ws.append(["Findings", ", ".join(f"{k} {v}" for k, v in counts.items())])
    ws.cell(row=ws.max_row, column=1).font = label_font
    ev = evidence_summary(ai.findings)
    ws.append(["Evidence check", f"Report quotes found: {ev['report_quote_verified']}/{ev['findings']}; "
                                 f"standard quotes found: {ev['standard_quote_verified']}/{ev['findings']}; "
                                 f"not found (needs review): {ev['not_found']}"])
    ws.cell(row=ws.max_row, column=1).font = label_font
    _band(ws, "Conclusion", hfill)
    ws.append([ai.conclusion])
    r = ws.max_row
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 5, end_column=6)
    ws.cell(row=r, column=1).alignment = WRAP
    for col, w in zip("ABCDEF", (24, 60, 70, 20, 20, 20)):
        ws.column_dimensions[col].width = w

    # ---- Findings ----
    ws_f = wb.create_sheet("Findings")
    _table(ws_f,
           ["Priority", "Clause", "Gap type", "Normative requirement", "Finding reason", "Required action", "Pages",
            "Verified by code", "Report quote", "Report quote check", "Standard quote", "Standard quote check"],
           [[f.severity, f.clause, f.gap_type, f.reason, f.finding, f.required_action, f.pages,
             "Yes" if f.code_verified else "", f.report_quote, f.report_check, f.standard_quote, f.standard_check]
            for f in ai.findings],
           [11, 12, 22, 50, 60, 45, 16, 10, 45, 22, 45, 22], hfill, risk_col=0)
    red = PatternFill("solid", fgColor="FFF8D7DA")
    for row in ws_f.iter_rows(min_row=2):
        for c in (row[9], row[11]):
            if c.value and "NOT FOUND" in str(c.value):
                c.fill = red

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()