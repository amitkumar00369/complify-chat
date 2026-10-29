"""Branded evaluation PDF (your own format) built with ReportLab - works on Windows/Linux/Mac.

Layout: portrait cover -> landscape content pages (wide tables) -> annex clause register.
Brand name / colour / logo / prepared-by come from settings (.env).
"""
import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, NextPageTemplate, PageBreak, PageTemplate,
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
    s = escape(str(text or ""))
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


RISK_BG = {"Critical": "#f8d7da", "High": "#fde5cd", "Medium": "#fff3cd", "Low": "#d1e7dd",
           "P0": "#f8d7da", "P1": "#fde5cd", "P2": "#fff3cd"}


# ---------------------------------------------------------------- page numbering
class _NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            if self._pageNumber > 1:
                w, _ = self._pagesize
                self.setFont(FONT, 7.5)
                self.setFillColor(colors.HexColor("#6b7682"))
                self.drawRightString(w - 14 * mm, 9 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


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

    P_W, P_H = A4
    L_W, L_H = landscape(A4)
    margin = 14 * mm

    def _cover_page(c, _d):
        c.saveState()
        c.setFillColor(brand)
        c.rect(0, P_H - 34 * mm, P_W, 34 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(FONT_B, 18)
        c.drawString(margin, P_H - 17 * mm, s.brand_name)
        c.setFont(FONT, 9.5)
        c.drawString(margin, P_H - 24 * mm, s.brand_tagline)
        if s.brand_logo_path and os.path.exists(s.brand_logo_path):
            c.drawImage(s.brand_logo_path, P_W - margin - 40 * mm, P_H - 30 * mm, 40 * mm, 24 * mm,
                        preserveAspectRatio=True, mask="auto")
        c.setFillColor(brand)
        c.rect(0, 0, P_W, 6 * mm, stroke=0, fill=1)
        c.restoreState()

    def _content_page(c, _d):
        c.saveState()
        w, h = c._pagesize
        c.setStrokeColor(brand)
        c.setLineWidth(1.2)
        c.line(margin, h - 11 * mm, w - margin, h - 11 * mm)
        c.setFont(FONT_B, 8.5)
        c.setFillColor(brand)
        c.drawString(margin, h - 9 * mm, s.brand_name)
        c.setFont(FONT, 7.8)
        c.setFillColor(colors.HexColor("#3a4652"))
        c.drawRightString(w - margin, h - 9 * mm,
                          f"Technical Evaluation — Report {ai.report_number} — {doc.evaluation_id[:8].upper()}")
        c.setFont(FONT, 7.2)
        c.setFillColor(colors.HexColor("#6b7682"))
        c.drawString(margin, 9 * mm, "Confidential — prepared for certification review. "
                                     "Findings relate only to the documents supplied.")
        c.restoreState()

    frame_p = Frame(margin, 12 * mm, P_W - 2 * margin, P_H - 50 * mm, id="cover")
    frame_l = Frame(margin, 14 * mm, L_W - 2 * margin, L_H - 28 * mm, id="land")
    pdf = BaseDocTemplate(buf, pagesize=A4, title=f"Technical Evaluation {ai.report_number}",
                          author=s.brand_name, subject=ai.title_product)
    pdf.addPageTemplates([
        PageTemplate(id="cover", frames=[frame_p], onPage=_cover_page, pagesize=A4),
        PageTemplate(id="land", frames=[frame_l], onPage=_content_page, pagesize=landscape(A4)),
    ])
    W = L_W - 2 * margin
    story = []

    # ---------------- cover
    story += [Spacer(1, 10 * mm), Paragraph("TECHNICAL EVALUATION REPORT", st["cover_title"]),
              Paragraph(_t(ai.title_product.upper()), st["cover_sub"]), Spacer(1, 8 * mm)]
    info = [
        ("Evaluation reference", doc.evaluation_id.upper()[:8] + " — " + doc.created_at.strftime("%d %B %Y")),
        ("Test report evaluated", f"{ai.report_number} — {ai.laboratory} — {ai.report_date}"),
        ("Report file", f"{doc.report_filename} ({doc.report_pages} pages)"),
        ("Product declared", ai.product_declared),
        ("Standards claimed", ai.standards_claimed),
        ("Standards evaluated", "; ".join(doc.standards) or "—"),
    ]
    t = Table([[Paragraph(_t(k), st["cellb"]), Paragraph(_t(v), st["body"])] for k, v in info],
              colWidths=[45 * mm, P_W - 2 * margin - 45 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dde5")),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [t, Spacer(1, 9 * mm)]
    bg, fg = _verdict_color(ai.final_verdict)
    vstyle = ParagraphStyle("vv", parent=st["verdict"], textColor=fg)
    vt = Table([[Paragraph("FINAL VERDICT", ParagraphStyle("vl", parent=vstyle, fontSize=8.5))],
                [Paragraph(_t(ai.final_verdict), vstyle)]], colWidths=[P_W - 2 * margin])
    vt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("BOX", (0, 0), (-1, -1), 1, fg),
                            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [vt, Spacer(1, 5 * mm), Paragraph(_t(ai.reason), st["body"]), Spacer(1, 14 * mm)]
    sign = Table([
        [Paragraph("Prepared by", st["cellb"]), Paragraph("Reviewed and approved by", st["cellb"]),
         Paragraph("Date", st["cellb"])],
        [Paragraph(_t(s.evaluation_prepared_by), st["body"]), Paragraph("Name / signature: ______________________", st["body"]),
         Paragraph(datetime.now().strftime("%d.%m.%Y"), st["body"])],
    ], colWidths=[(P_W - 2 * margin) * f for f in (0.34, 0.46, 0.20)])
    sign.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d3de")),
                              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3f6")),
                              ("TOPPADDING", (0, 1), (-1, 1), 10), ("BOTTOMPADDING", (0, 1), (-1, 1), 10)]))
    story += [sign, NextPageTemplate("land"), PageBreak()]

    # ---------------- 1. summary
    story.append(Paragraph("1. Evaluation Summary", st["h1"]))
    summary = [("Report number", f"{ai.report_number} | {ai.laboratory} | {ai.report_date}"),
               ("Product declared", ai.product_declared), ("Standards claimed", ai.standards_claimed),
               ("Evaluation basis", ai.evaluation_basis), ("Final verdict", ai.final_verdict),
               ("Reason", ai.reason)]
    story.append(_table(st, brand, ["Item", "Detail"], summary, [48 * mm, W - 48 * mm]))

    story.append(Paragraph("2. Report-Validity Decision Points", st["h1"]))
    story.append(_table(st, brand, ["Priority", "Decision point", "Finding", "Required resolution"],
                        [[d.priority, d.title, d.finding, d.required_resolution] for d in ai.decision_points],
                        [17 * mm, 48 * mm, (W - 65 * mm) * 0.55, (W - 65 * mm) * 0.45], risk_cols=(0,)))

    story += [PageBreak(), Paragraph("3. Critical Findings", st["h1"])]
    story.append(_table(st, brand,
                        ["Clause / concern", "Report condition", "Evaluation comments", "Required verdict",
                         "Required action", "Reference"],
                        [[f.clause_or_concern, f.report_condition, f.engineer_comments, f.required_verdict,
                          f.required_action, f.report_reference] for f in ai.critical_findings],
                        [W * x for x in (0.13, 0.20, 0.22, 0.12, 0.23, 0.10)], color_cols=(3,)))

    story += [PageBreak(), Paragraph(f"4. {doc.part2_label} Coverage", st["h1"])]
    story.append(_table(st, brand,
                        ["Clause(s)", "Safety area", "Report status", "Applicability rule",
                         "Required report outcome", "Risk"],
                        [[c.clauses, c.safety_area, c.report_status, c.applicability_rule,
                          c.required_outcome, c.risk_level] for c in ai.part2_coverage],
                        [W * x for x in (0.11, 0.17, 0.20, 0.21, 0.23, 0.08)], risk_cols=(5,)))

    story.append(Paragraph("5. Part 1 Corrections", st["h1"]))
    story.append(_table(st, brand,
                        ["Clause / issue", "Evaluation finding", "Evaluation comments", "Required correction",
                         "Final status"],
                        [[c.clause_or_issue, c.finding, c.engineer_comments, c.required_correction,
                          c.final_status] for c in ai.part1_corrections],
                        [W * x for x in (0.15, 0.23, 0.25, 0.23, 0.14)], color_cols=(4,)))

    story.append(KeepTogether([Paragraph("6. Final Technical Conclusion", st["h1"]),
                               Paragraph(_t(ai.final_technical_conclusion), st["body"])]))

    # ---------------- annex
    story += [PageBreak(), Paragraph("Annex A — Clause-by-Clause Register", st["h1"]),
              Paragraph(_t(f"{len(doc.clause_review)} clause rows extracted from the test report. "
                           "Assessment uses a fixed vocabulary; clauses marked CRITICAL carry a specific "
                           "verification note."), st["small"]), Spacer(1, 3 * mm)]
    story.append(_table(st, brand,
                        ["Clause", "Report row / topic", "Verdict", "Assessment", "Required verification",
                         "Page"],
                        [[r.clause, r.topic, r.report_verdict, r.assessment, r.required_verification,
                          r.pdf_page] for r in doc.clause_review],
                        [W * x for x in (0.07, 0.31, 0.06, 0.22, 0.24, 0.10)]))

    pdf.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
