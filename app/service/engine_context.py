"""effective_kb.json + report_evidence.json (from the compliance engine) -> compact text for the LLM.

Why not send the JSON files as they are: effective_kb.json is ~5.5 MB and report_evidence.json ~1.4 MB
(parser metadata, provenance, applicability logic, repeated sub-rows). The model only needs the words of
the standard and of the report, with clause numbers and page numbers. This module keeps exactly that:

STANDARD  every clause of the effective (merged) standard, in clause order:
          [7.1] Marking ... | Part 2: ADDITION | P1 p.24-25, P2 p.13
          <Part 1 text>
          + Part 2 ADDITION: <Part 2 text>
          plus annexes, tables and definitions.
REPORT    front matter (cover, ratings, model) + every report row with its clause, verdict and page.

Generic: works for any standard family (base + Part 2, or standalone) and any report layout the engine
parsed. Nothing here is product- or standard-specific except the watermark filter, which only removes
lines that are the same licence/footer text repeated on many pages.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# ------------------------------------------------------------------ text cleanup
_PAGE_FOOTER_RE = re.compile(r"^\s*(?:[–-]\s*\d+\s*[–-]|page\s+\d+\s+of\s+\d+)\s*$", re.I)


def _repeated_lines(texts: list[str], min_count: int = 6) -> set[str]:
    """Lines that repeat on many pages = licence watermarks, running headers ('60335-1 © IEC:2010')."""
    c = Counter(_ws(line) for t in texts for line in {_ws(x) for x in (t or "").splitlines()} if len(line) > 8)
    return {line for line, n in c.items() if n >= min_count}


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def clean(text: str | None, noise: set[str] = frozenset()) -> str:
    out = []
    for line in (text or "").splitlines():
        s = _ws(line)
        if not s or s in noise or _PAGE_FOOTER_RE.match(s):
            continue
        out.append(s)
    return "\n".join(out)


def _pages(start, end) -> str:
    if not start:
        return ""
    return f"p.{start}" if not end or end == start else f"p.{start}-{end}"


def _clause_key(cid: str) -> tuple:
    """'19.11.4.2' sorts after '19.2'; 'AA.1' after numbers."""
    parts = re.split(r"[.\s]", cid or "")
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


@dataclass
class BuiltContext:
    standard_text: str
    report_text: str
    standard_labels: list[str]                      # ["IEC 60335-1:2010 (Part 1)", "IEC 60335-2-7:2012 (Part 2)"]
    report_number: str
    # for the quote check: label -> {page: text}
    standard_pages: dict[str, dict[int, str]] = field(default_factory=dict)
    report_pages: dict[int, str] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.standard_text) + len(self.report_text)

    @property
    def est_tokens(self) -> int:
        return self.chars // 4                         # English technical text ~4 chars/token


# ------------------------------------------------------------------ standard
def _std_label(meta: dict | None, role: str) -> str:
    if not meta:
        return ""
    num = meta.get("standard_number") or meta.get("standard_id") or "?"
    year = meta.get("year")
    return f"{num}{':' + str(year) if year else ''} ({role})"


def _walk(nodes):
    for n in nodes or []:
        yield n
        yield from _walk(n.get("children"))


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", (s or "").lower())


def build_standard_text(kb: dict) -> tuple[str, list[str], dict[str, dict[int, str]]]:
    base_meta, delta_meta = kb.get("base_standard"), kb.get("delta_standard")
    has_delta = bool(delta_meta)
    lbl_base = _std_label(base_meta, "Part 1" if has_delta else "standard")
    lbl_delta = _std_label(delta_meta, "Part 2") if has_delta else ""
    labels = [x for x in (lbl_base, lbl_delta) if x]
    short = {"BASE": "P1", "DELTA": "P2"}

    nodes = list(_walk(kb.get("clauses")))
    noise = _repeated_lines([n.get("raw_text") or "" for n in nodes]
                            + [(t.get("source") or {}).get("raw_text") or "" for t in kb.get("tables") or []])
    pages: dict[str, dict[int, str]] = {lbl_base: {}, **({lbl_delta: {}} if lbl_delta else {})}

    def add_page(label: str, page, text: str):
        if label and page and text:
            d = pages.setdefault(label, {})
            d[int(page)] = (d.get(int(page), "") + "\n" + text).strip()

    lines = [f"# STANDARD: {' + '.join(labels)}",
             "Effective (merged) text built by code from the standard PDF(s). Each clause: "
             "[clause] title | Part 2 operation | page in Part 1 (P1) / Part 2 (P2). "
             "Lines starting with '+ Part 2' are the Part 2 text that adds to, replaces or modifies the clause.",
             ""]
    for n in sorted(nodes, key=lambda x: _clause_key(x.get("clause_id", ""))):
        cid = n.get("clause_id") or "?"
        ops = sorted({o.get("operation_type") for o in n.get("applied_operations") or [] if o.get("operation_type")})
        bsrc = (n.get("base_sources") or [{}])[0]
        dsrc = (n.get("delta_sources") or [{}])[0]
        where = ", ".join(x for x in (
            f"{short['BASE']} {_pages(bsrc.get('page_start'), bsrc.get('page_end'))}" if bsrc.get("page_start") else "",
            f"{short['DELTA']} {_pages(dsrc.get('page_start'), dsrc.get('page_end'))}" if dsrc.get("page_start") else "",
        ) if x)
        head = f"[{cid}] {(n.get('title') or '').strip()}"
        if ops:
            head += f" | Part 2: {', '.join(ops)}"
        if n.get("effective_status") == "INACTIVE":
            head += " | NOT APPLICABLE (Part 2)"
        if where:
            head += f" | {where}"
        body = clean(n.get("raw_text"), noise)
        # the clause text comes from Part 2 when it was replaced / is new
        from_delta = not bsrc.get("page_start") or bool({"REPLACEMENT", "NEW_REQUIREMENT"} & set(ops))
        add_page(lbl_delta if from_delta and lbl_delta else lbl_base,
                 (dsrc if from_delta and lbl_delta else bsrc).get("page_start"), body)
        extra = []
        seen = _norm(body)
        for r in n.get("requirements") or []:
            src = r.get("effective_source_type") or "BASE"
            if src == "BASE" or not r.get("text"):
                continue
            t = clean(r["text"], noise)
            if not t or _norm(t)[:80] in seen:                  # already part of the clause text
                continue
            seen += _norm(t)
            status = "" if r.get("effective_status") in (None, "ACTIVE") else f" ({r['effective_status']})"
            extra.append(f"+ Part 2 {src.replace('PART_2_', '')}{status}: {t}")
            add_page(lbl_delta, dsrc.get("page_start"), t)
        lines.append(head)
        if body:
            lines.append(body)
        lines.extend(extra)
        lines.append("")

    annexes = kb.get("annexes") or []
    if annexes:
        lines.append("## ANNEXES")
        for a in annexes:
            src = (a.get("delta_sources") or a.get("base_sources") or [{}])[0]
            part = "P2" if a.get("delta_sources") else "P1"
            lines.append(f"[Annex {a.get('annex_id')}] {(a.get('title') or '').strip()}"
                         f"{' | NOT APPLICABLE' if a.get('effective_status') == 'INACTIVE' else ''}"
                         f" | {part} {_pages(src.get('page_start'), src.get('page_end'))}")
            for r in a.get("requirements") or []:
                t = clean(r.get("text"), noise)
                if t:
                    lines.append(f"{r.get('clause_id') or ''} {t}".strip())
                    add_page(lbl_delta if part == "P2" else lbl_base, src.get("page_start"), t)
            lines.append("")

    tables = kb.get("tables") or []
    if tables:
        lines.append("## TABLES")
        for t in tables:
            src = t.get("source") or {}
            body = clean(src.get("raw_text"), noise)
            if body:
                lines.append(f"[Table {t.get('table_id')}] {_pages(src.get('page_start'), src.get('page_end'))}")
                lines.append(body)
                lines.append("")
                add_page(lbl_base, src.get("page_start"), body)

    defs = kb.get("definitions") or []
    if defs:
        lines.append("## DEFINITIONS")
        for d in defs:
            if d.get("term"):
                lines.append(f"{d.get('clause_id') or ''} {d['term']}: {clean(d.get('definition'), noise)}".strip())
        lines.append("")
    return "\n".join(lines), labels, pages


# ------------------------------------------------------------------ report
_SKIP_EVIDENCE = {"TABLE_VALUE"}                       # derived copies of table rows ('Measured X: 2500 W')


def build_report_text(ev: dict) -> tuple[str, dict[int, str]]:
    nodes = list(_walk(ev.get("clauses")))
    rows = []
    for n in nodes:
        for e in n.get("evidence_items") or []:
            if e.get("parent_evidence_id") or e.get("evidence_type") in _SKIP_EVIDENCE:
                continue
            rows.append((n, e))
    front = ev.get("unmapped_sections") or []
    noise = _repeated_lines([e.get("raw_text") or "" for _, e in rows] + [s.get("raw_text") or "" for s in front], 8)
    pages: dict[int, str] = {}

    def add_page(p, text):
        if p and text:
            pages[int(p)] = (pages.get(int(p), "") + "\n" + text).strip()

    lines = [f"# REPORT: {ev.get('file_name') or ''} | report number {ev.get('report_number') or 'not stated'}",
             "Text extracted by code from the report PDF. Page numbers are report PDF pages. "
             f"Standards the report names: {', '.join(ev.get('standard_numbers') or []) or 'none found'}.",
             ""]
    if front:
        lines.append("## FRONT MATTER (cover, product, ratings, laboratory)")
        for s in front:
            body = clean(s.get("raw_text"), noise)
            if body:
                lines.append(f"({_pages(s.get('page_start'), s.get('page_end'))})")
                lines.append(body)
                add_page(s.get("page_start"), body)
        lines.append("")

    lines.append("## CLAUSE ROWS (in report order; verdict as written in the report)")
    rows.sort(key=lambda x: (x[1].get("page_start") or 0, x[1].get("source_order") or 0))
    for n, e in rows:
        body = clean(e.get("raw_text"), noise)
        if not body:
            continue
        verdict = e.get("raw_verdict_text") or e.get("reported_verdict") or ""
        lines.append(f"[{n.get('clause_id')}] {_pages(e.get('page_start'), e.get('page_end'))}"
                     f"{' | verdict ' + verdict if verdict and verdict != 'NOT_STATED' else ''}")
        lines.append(body)
        add_page(e.get("page_start"), body)

    diags = [d for d in ev.get("diagnostics") or [] if d.get("code") == "REPORT_VERDICT_AMBIGUOUS"]
    if diags:
        lines.append("")
        lines.append("## EXTRACTION NOTES (code)")
        for d in diags:
            lines.append(f"[{d.get('clause_id')}] p.{d.get('page')}: several verdicts on one row: "
                         f"{', '.join(d.get('values') or [])}")
    return "\n".join(lines), pages


def build_context(effective_kb: dict, report_evidence: dict) -> BuiltContext:
    std_text, labels, std_pages = build_standard_text(effective_kb)
    rep_text, rep_pages = build_report_text(report_evidence)
    return BuiltContext(standard_text=std_text, report_text=rep_text, standard_labels=labels,
                        report_number=report_evidence.get("report_number") or "",
                        standard_pages=std_pages, report_pages=rep_pages)