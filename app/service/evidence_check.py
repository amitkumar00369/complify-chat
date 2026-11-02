"""Code check of the AI's evidence: is every quote really in the report / the supplied standards?

The AI gives, per finding, a short verbatim quote from the report and one from the standard.
This module searches those quotes in the PDF text (no AI) and writes the result on the finding:

  "verified p.14"            quote found on a page the finding cites (report) / in a supplied standard
  "found on p.22 (not cited)" quote found, but on another page than the finding cites
  "approximate p.14"         >= 80 % of the quote's words found together (OCR/layout differences)
  "NOT FOUND"                quote not in the document -> finding needs human review
  "no quote given"           the AI gave no quote

Works for any standard body (IEC, EN, UL, ISO, GSO, ...): it only compares text.
Text inside images (photos, scanned pages) cannot be checked and shows as NOT FOUND.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

_CACHE: dict[str, list[str]] = {}


def page_texts(data: bytes) -> list[str]:
    """Plain text per page (pypdf: fast enough for 300+ page standards), cached by file hash."""
    sha = hashlib.sha256(data).hexdigest()
    if sha not in _CACHE:
        from app.utils.pdf_utils import pdf_page_texts   # handles encrypted PDFs, never raises

        _CACHE[sha] = [_norm(t) for t in pdf_page_texts(data)]
    return _CACHE[sha]


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "−": "-",
                                   " ": " ", " ": " ", " ": " ", "×": "x"}))
    s = re.sub(r"(?<=\d)[ ,](?=\d{3}\b)", "", s)          # 50 000 / 1,000 -> 50000 / 1000
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)                  # 0,1 -> 0.1 (IEC decimal comma)
    s = re.sub(r"[^a-z0-9.%°ω/+-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pages(ref: str) -> list[int]:
    out: list[int] = []
    for a, b in re.findall(r"(\d+)(?:\s*[–-]\s*(\d+))?", ref or ""):
        out += list(range(int(a), int(b) + 1)) if b and 0 <= int(b) - int(a) < 50 else [int(a)]
    return out


@lru_cache(maxsize=4096)
def _words(s: str) -> tuple[str, ...]:
    return tuple(w for w in s.split() if len(w) > 1 or w.isdigit())


def _approx(q: str, page: str) -> bool:
    """>= 80 % of the quote's words appear within one window of the page text."""
    qw = _words(q)
    if len(qw) < 4:
        return False
    pw = _words(page)
    need = set(qw)
    if len(need & set(pw)) < 0.8 * len(need):
        return False
    n = len(qw) + 6
    for i in range(0, max(1, len(pw) - n + 1), 3):
        if len(need & set(pw[i:i + n])) >= 0.8 * len(need):
            return True
    return False


def _hit(q: str, t: str) -> bool:
    """Exact match, or exact match ignoring spaces (PDF text often loses or adds spaces between words)."""
    return q in t or (len(q) >= 12 and q.replace(" ", "") in t.replace(" ", ""))


def find_quote(quote: str, docs: list[tuple[str, list[str]]], cited: list[int] | None = None) -> str:
    """docs = [(label, normalised page texts)]. cited = report pages the finding names (report only)."""
    q = _norm(quote).strip(" .\"'")
    if not q or q in ("not stated", "none", "n a", "-", "not given") or len(q) < 6:
        return "no quote given"
    cited = cited or []
    for label, pages in docs:
        for p in cited:
            for pp in (p, p - 1, p + 1):                    # +/-1: PDF page vs printed page labels
                if 1 <= pp <= len(pages) and _hit(q, pages[pp - 1]):
                    return f"verified{_lbl(label)} p.{pp}"
    for label, pages in docs:
        for i, t in enumerate(pages, start=1):
            if _hit(q, t):
                return f"verified{_lbl(label)} p.{i}" if not cited else f"found{_lbl(label)} on p.{i} (not cited)"
    for label, pages in docs:
        for i, t in enumerate(pages, start=1):
            if _approx(q, t):
                return f"approximate{_lbl(label)} p.{i}"
    return "NOT FOUND"


def _lbl(label: str) -> str:
    return f" in {label}" if label else ""


def verify_findings(ai, report_pdf: bytes | None, standards: list[tuple[str, bytes]]):
    """Fill report_check / standard_check on every AI finding (code rows are already verified)."""
    rep = [("", page_texts(report_pdf))] if report_pdf else []
    stds = [(label, page_texts(data)) for label, data in standards]
    for f in ai.findings:
        if f.code_verified and not f.report_quote:
            f.report_check = f.report_check if f.report_check not in ("", "not checked") else "checked by code"
            continue
        f.report_check = find_quote(f.report_quote, rep, _pages(f.pages)) if rep else "report not available"
        if f.standard_quote.strip().lower().startswith("not in supplied"):
            f.standard_check = "requirement not in supplied standards"
        else:
            f.standard_check = find_quote(f.standard_quote, stds) if stds else "standard not available"
    return ai


def evidence_summary(findings) -> dict[str, int]:
    def ok(s: str) -> bool:
        return s.startswith(("verified", "found", "approximate", "checked by code"))
    return {
        "report_quote_verified": sum(ok(f.report_check) for f in findings),
        "standard_quote_verified": sum(ok(f.standard_check) for f in findings),
        "not_found": sum("NOT FOUND" in (f.report_check + f.standard_check) for f in findings),
        "findings": len(findings),
    }