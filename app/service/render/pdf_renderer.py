"""Branded evaluation PDF (your own format) built with ReportLab - works on Windows/Linux/Mac.

Layout (landscape): 1. Evaluation output  2. Report identity  3. Summary  4. Findings.
Brand name / colour / logo / prepared-by come from settings (.env).
"""
import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

from app.config.settings import get_settings
from app.schema.evaluation import EvaluationDocument

# ---------------------------------------------------------------- fonts
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
FONT, FONT_B = "Helvetica", "Helvetica-Bold"
for reg, bold in _FONT_CANDIDATES:
    if os.path.exists(reg) and os.path.exists(bold):
        pdfmetrics.registerFont(TTFont("EvalSans", reg))
        pdfmetrics.registerFont(TTFont("EvalSans-Bold", bold))
        FONT, FONT_B = "EvalSans", "EvalSans-Bold"
        break

_ASCII_FALLBACK = {"≤": "<=", "≥": ">=", "Δ": "delta ", "−": "-", "→": "->", "×": "x", "±": "+/-"}


def _t(text) -> str:
    s = escape(str(text or "")).replace("\n", "<br/>")
    if FONT == "Helvetica":
        for k, v in _ASCII_FALLBACK.items():
            s = s.replace(k, v)
    return s


# ---------------------------------------------------------------- styles
def _styles(brand):
    base = dict(fontName=FONT, fontSize=8.6, leading=11)
    return {
        "body": ParagraphStyle("body", **base),
        "small": ParagraphStyle("small", fontName=FONT, fontSize=7.4, leading=9.2),
        "cell": ParagraphStyle("cell", fontName=FONT, fontSize=7.6, leading=9.4),
        "cellb": ParagraphStyle("cellb", fontName=FONT_B, fontSize=7.6, leading=9.4),
        "th": ParagraphStyle("th", fontName=FONT_B, fontSize=7.8, leading=9.6, textColor=colors.white),
        "h1": ParagraphStyle("h1", fontName=FONT_B, fontSize=13, leading=16, textColor=brand,
                             spaceBefore=4, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName=FONT_B, fontSize=10, leading=13, textColor=brand,
                             spaceBefore=8, spaceAfter=4),
        "cover_title": ParagraphStyle("ct", fontName=FONT_B, fontSize=22, leading=27, textColor=brand),
        "cover_sub": ParagraphStyle("cs", fontName=FONT, fontSize=12, leading=16,
                                    textColor=colors.HexColor("#3a4652")),
        "verdict": ParagraphStyle("v", fontName=FONT_B, fontSize=10.5, leading=14, alignment=TA_CENTER),
    }


def _verdict_color(text: str):
    t = (text or "").upper()
    if any(k in t for k in ("HOLD", "REJECT", "FAIL", "NON-CONFORMITY", "REPLACEMENT")):
        return colors.HexColor("#f8d7da"), colors.HexColor("#842029")
    if any(k in t for k in ("CONDITIONAL", "INSUFFICIENT", "REVIEW", "CORRECT", "MINOR", "COVERAGE NOT")):
        return colors.HexColor("#fff3cd"), colors.HexColor("#664d03")
    return colors.HexColor("#d1e7dd"), colors.HexColor("#0f5132")


RISK_BG = {"Critical": "#f8d7da", "High": "#fde5cd", "Medium": "#fff3cd", "Low": "#d1e7dd"}


# ---------------------------------------------------------------- table helper
def _table(st, brand, headers, rows, widths, color_cols=(), risk_cols=(), bold_first=True):
    data = [[Paragraph(_t(h), st["th"]) for h in headers]]
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), brand),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d3de")),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5), ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fa")]),
    ]
    for i, r in enumerate(rows, start=1):
        cells = []
        for j, v in enumerate(r):
            cells.append(Paragraph(_t(v), st["cellb"] if (bold_first and j == 0) or j in color_cols else st["cell"]))
            if j in color_cols:
                bg, _fg = _verdict_color(str(v))
                style.append(("BACKGROUND", (j, i), (j, i), bg))
            if j in risk_cols and str(v) in RISK_BG:
                style.append(("BACKGROUND", (j, i), (j, i), colors.HexColor(RISK_BG[str(v)])))
        data.append(cells)
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


# ---------------------------------------------------------------- main
def render_pdf(doc: EvaluationDocument) -> bytes:
    s = get_settings()
    brand = colors.HexColor(s.brand_color)
    st = _styles(brand)
    ai = doc.ai
    buf = io.BytesIO()

    L_W, L_H = landscape(A4)
    margin = 14 * mm

    frame_l = Frame(margin, 12 * mm, L_W - 2 * margin, L_H - 24 * mm, id="land")      # no header / footer
    pdf = BaseDocTemplate(buf, pagesize=landscape(A4), title=f"Technical Evaluation {ai.report_number}",
                          author=s.brand_name, subject=ai.title_product)
    pdf.addPageTemplates([PageTemplate(id="land", frames=[frame_l], pagesize=landscape(A4))])
    W = L_W - 2 * margin
    KW = 48 * mm                                         # key column of the key/value tables

    def _kv(rows):
        return _table(st, brand, ["Field", "Value"], rows, [KW, W - KW])

    story = [Paragraph(_t(f"TECHNICAL EVALUATION — {ai.title_product.upper()}"), st["cover_title"]),
             Spacer(1, 4 * mm)]

    # ---------------- 1. evaluation output (what was evaluated, when, with what)
    story.append(Paragraph("1. Evaluation output", st["h1"]))
    story.append(_kv([
        ("Report", f"{doc.report_filename} ({doc.report_pages} pages)"),
        ("Standards", "; ".join(doc.standards) or "—"),
        ("Created", doc.created_at.strftime("%Y-%m-%d %H:%M UTC")),
    ]))

    # ---------------- 2. report identity (from the AI answer)
    story.append(Paragraph("2. Report identity", st["h1"]))
    story.append(_kv([
        ("Report number", ai.report_number), ("Laboratory", ai.laboratory), ("Report date", ai.report_date),
        ("Product declared", ai.product_declared), ("Declared model", ai.declared_model),
        ("Standards claimed", ai.standards_claimed), ("Standards evaluated", ai.standards_evaluated),
    ]))

    # ---------------- 3. summary
    story.append(Paragraph("3. Summary", st["h1"]))
    counts = {lvl: sum(f.severity == lvl for f in ai.findings) for lvl in ("Critical", "High", "Medium", "Low")}
    if any(f.severity == "Not stated" for f in ai.findings):
        counts["Priority not stated"] = sum(f.severity == "Not stated" for f in ai.findings)
    summary = _table(st, brand, ["Field", "Value"], [
        ("Final verdict", ai.final_verdict),
        ("Reason", ai.reason),
        ("Recommended action", ai.recommended_action),
        ("Conclusion", ai.conclusion),
        ("Findings", f"{len(ai.findings)} total — " + ", ".join(f"{k} {v}" for k, v in counts.items())),
    ], [KW, W - KW])
    bg, fg = _verdict_color(ai.final_verdict)
    summary.setStyle(TableStyle([("BACKGROUND", (1, 1), (1, 1), bg), ("TEXTCOLOR", (1, 1), (1, 1), fg)]))
    story.append(summary)

    # ---------------- 4. findings
    story += [PageBreak(), Paragraph("4. Findings", st["h1"])]
    if ai.findings:
        story.append(_table(st, brand, ["Priority", "Clause", "Normative requirement", "Finding reason",
                                        "Required action", "Pages", "Evidence"],
                            [[f.severity, f.clause, f.reason, f.finding, f.required_action, f.pages,
                              f"Report: {f.report_check}\nStandard: {f.standard_check}"] for f in ai.findings],
                            [W * x for x in (0.07, 0.08, 0.22, 0.27, 0.19, 0.06, 0.11)], risk_cols=(0,)))
    else:
        story.append(Paragraph("No findings in the evaluation.", st["body"]))

    pdf.build(story)
    return buf.getvalue()