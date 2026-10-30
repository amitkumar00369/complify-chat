"""Build the evaluation workbook data from a /chat markdown answer - NO LLM call.

Reads the answer's markdown tables (Finding | Classification | Standard reference | Evidence),
"Priority actions" list, overall conclusion and disposition, and maps them to EvaluationAI
(same object the AI would return), so the same Excel/PDF renderers are used.
"""
import re
from typing import get_args

from app.schema.evaluation import (
    ClauseNote, CoverageRow, CriticalFinding, DecisionPoint, EvaluationAI, Part1Correction, ProductFact,
)
from app.schema.evaluation import FinalVerdict

NOT_STATED = "not stated in review"

VERDICT_BY_CLASS = {
    "non-compliant": "HOLD / CORRECT",
    "missing evidence": "INSUFFICIENT DETAIL",
    "compliant": "PASS WITH EVIDENCE",
    "not applicable": "PASS OR JUSTIFIED N/A",
}
STATUS_BY_CLASS = {
    "non-compliant": "REPORT HOLD",
    "missing evidence": "COVERAGE NOT VERIFIED",
    "compliant": "PASS WITH EVIDENCE",
    "not applicable": "PASS OR JUSTIFIED N/A",
}
ACTION_BY_CLASS = {
    "non-compliant": "Correct the assessment and repeat the test under the specified conditions.",
    "missing evidence": "Provide the missing test records and applicability justification.",
    "compliant": "Retain evidence with sample traceability.",
    "not applicable": "Retain N/A justification.",
}
_IMPERATIVE = re.compile(
    r"(?:^|(?<=[.;]\s))((?:Provide|Confirm|Resolve|Determine|Identify|Establish|Supply|Correct|"
    r"Apply|Verify|Address|Reconcile|Repeat|Retain)\b(?:[^.]|\.(?=\d))*\.)")
_CLAUSE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){0,4})(?:\s*[–-]\s*(\d{1,3}(?:\.\d{1,3}){0,4}))?(?![\w.])")


# ------------------------------------------------------------------ helpers
def _plain(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text or "")
    text = text.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def _sections(md: str) -> dict[str, str]:
    """'## 1. Demonstrated ...' -> body (keyed by lower-case heading text)."""
    out, current, buf = {}, "_top", []
    for line in md.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)$", line)
        if m:
            out[current] = "\n".join(buf)
            current, buf = _plain(m.group(1)).lower(), []
        else:
            buf.append(line)
    out[current] = "\n".join(buf)
    return out


def _tables(md: str) -> list[list[dict[str, str]]]:
    tables, rows, header = [], [], None
    for line in md.splitlines() + [""]:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if header is None:
                header = [_plain(c).lower() for c in cells]
            elif all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            else:
                rows.append(dict(zip(header, cells)))
        elif header is not None:
            tables.append(rows)
            rows, header = [], None
    return tables


def _pick(row: dict[str, str], *keys: str) -> str:
    for k in row:
        if any(key in k for key in keys):
            return row[k]
    return ""


_PG = r"\d+(?![.\d])(?:\s*[–-]\s*\d+(?![.\d]))?"
_PAGE_LIST = re.compile(rf"[Pp]ages?\s+({_PG}(?:\s*(?:,|and)\s*{_PG})*)")


def _pages(evidence: str) -> tuple[str, str]:
    """'**Pages 2, 4 and 77:** text' -> ('PDF pp.2,4,77', 'text').
    '**Page 18, transition from 15.2 to 15.3:**' -> ('PDF p.18', ...)  (clause numbers ignored)"""
    m = re.match(r"\s*\*\*(Pages?\s+[^*]+?):?\*\*:?\s*(.*)$", evidence, re.S)
    if not m:
        return "", _plain(evidence)
    nums: list[str] = []
    for lst in _PAGE_LIST.findall(m.group(1)):
        nums += [re.sub(r"\s", "", n).replace("-", "–") for n in re.findall(_PG, lst)]
    if not nums:
        return "", _plain(evidence)
    ref = ("PDF p." if len(nums) == 1 and "–" not in nums[0] else "PDF pp.") + ",".join(nums)
    return ref, _plain(m.group(2))


def _clauses(ref: str) -> tuple[list[str], list[str]]:
    """'P1 10.1, Table 1; P2 10.1, 13.1–13.3' -> (part1 clauses, part2 clauses)."""
    p1, p2, target = [], [], None
    for chunk in re.split(r"[;]", _plain(ref)):
        chunk = re.sub(r"\b(Tables?|Annex|Figure)\s+[A-Z0-9]+(?:\s*[–-]\s*[A-Z0-9]+)?", "", chunk)
        if "P1" in chunk:
            target = p1
        if "P2" in chunk:
            target = p2
        chunk = chunk.replace("P1", "").replace("P2", "")
        for a, b in _CLAUSE.findall(chunk):
            found = [a]
            if b:  # expand small ranges: 20.101–20.105
                ha, hb = a.rsplit(".", 1) if "." in a else ("", a), b.rsplit(".", 1) if "." in b else ("", b)
                if ha[0] == hb[0] and ha[1].isdigit() and hb[1].isdigit() and 0 < int(hb[1]) - int(ha[1]) <= 20:
                    found = [f"{ha[0]}.{i}" if ha[0] else str(i) for i in range(int(ha[1]), int(hb[1]) + 1)]
                else:
                    found = [a, b]
            (target if target is not None else p1).extend(found)
    dedupe = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    return dedupe(p1), dedupe(p2)


def _split_first(text: str) -> tuple[str, str]:
    """First sentence = what the report records; the rest = engineering comment."""
    if not text:
        return NOT_STATED, NOT_STATED
    parts = re.split(r"(?<=[.])\s+(?=[A-Z])", text, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (text, "—")


def _root_cause(explicit: str, name: str, cls: str, p1: list[str], p2: list[str]) -> str:
    m = re.search(r"P0-0[1-5]|P[12]\b", explicit or "")
    if m:
        return m.group(0)
    if cls in ("compliant", "not applicable"):
        return "P2"
    tops = {c.split(".")[0] for c in p1 + p2}
    if re.search(r"identity|traceab|declared versus tested|sample", name, re.I):
        return "P0-01"
    if "10" in tops and re.search(r"power|input|clause 10|current", name, re.I):
        return "P0-03"
    if "11" in tops and re.search(r"heating|temperature (rise|limit)|thermal", name, re.I):
        return "P0-04"
    if "19" in tops:
        return "P0-05"
    if any(c.count(".") >= 1 and int(c.split(".")[-1]) >= 100 for c in p2 if c.split(".")[-1].isdigit()):
        return "P0-02"
    return "P1"


def _page_ref(cell: str) -> str:
    """'77, 79' / 'PDF pp.77,79' / 'Pages 13–15 and 77' -> 'PDF pp.77,79'."""
    cell = _plain(cell)
    if cell.upper().startswith("PDF"):
        return cell
    nums = [re.sub(r"\s", "", n).replace("-", "–") for n in re.findall(_PG, cell)]
    if not nums:
        return cell or NOT_STATED
    return ("PDF p." if len(nums) == 1 and "–" not in nums[0] else "PDF pp.") + ",".join(nums)


def _action(text: str, cls: str) -> str:
    acts = _IMPERATIVE.findall(text)
    return " ".join(acts) if acts else ACTION_BY_CLASS.get(cls, "Review.")


def _first(pattern: str, text: str, default: str = NOT_STATED) -> str:
    m = re.search(pattern, text, re.I | re.M)
    return _plain(m.group(1)) if m else default


# ------------------------------------------------------------------ main
def parse_chat_markdown(md: str, report_meta: dict[str, str] | None = None) -> EvaluationAI:
    report_meta = report_meta or {}
    sec = _sections(md)
    find = lambda key: next((v for k, v in sec.items() if key in k), "")  # noqa: E731

    report_meta = dict(report_meta)
    report_no = _first(r"\*\*Report:\*\*\s*([^\s—-]+)", md, report_meta.get("report_number", NOT_STATED))
    product = _first(r"\*\*Product declared:\*\*\s*(.+)$", md)
    declared_model = _first(r"model\s+([A-Z0-9][\w/.-]+)", product, "DECLARED MODEL").rstrip(",.")
    standards = _first(r"^-\s*Standards:\s*(.+)$", md)
    title = _first(r"declared:\*\*\s*([^,]+?),\s*model", md, "TEST REPORT").upper()

    overall = _plain(find("overall conclusion"))
    disposition = _plain(find("required disposition").split("Priority actions")[0])
    limitation = _plain(find("standard-document limitation").split("Abbreviations")[0])

    disp_low = (disposition + " " + overall).lower()
    if "do not rely" in disp_low or "return the report" in disp_low or "not provide an adequate basis" in disp_low:
        verdict = "REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD"
    elif "non-conform" in disp_low and "established" in disp_low:
        verdict = "REPORT REJECTED — NON-CONFORMITY ESTABLISHED"
    elif "minor" in disp_low:
        verdict = "REPORT ACCEPTED SUBJECT TO MINOR CORRECTIONS"
    else:
        verdict = "REPORT ACCEPTED — PRODUCT COMPLIANCE DEMONSTRATED"

    # ---- decision points from "Priority actions" (1st two = P0, rest = P1)
    decisions = []
    for i, m in enumerate(re.finditer(r"^\s*\d+\.\s+(.+)$", find("required disposition"), re.M)):
        item = m.group(1)
        bm = re.match(r"\*\*(.+?):?\*\*:?\s*(.*)$", item)
        title_dp = _plain(bm.group(1)).rstrip(":") if bm else _plain(item)[:60]
        rest = _plain(bm.group(2)).lstrip(" ,:;-—") if bm and bm.group(2) else ""
        # "**Correct power tests**, including ..." -> keep the whole sentence
        detail = rest if rest and rest[:1].isupper() else _plain(item)
        decisions.append(DecisionPoint(priority="P0" if i < 2 else "P1", title=title_dp,
                                       finding=detail, required_resolution=_plain(item)))

    # ---- table rows -> findings / coverage / corrections / clause notes
    critical, coverage, corrections = [], [], []
    notes: dict[str, ClauseNote] = {}
    identity_conflict = False
    product_facts: list[ProductFact] = []
    identity: dict[str, str] = {}
    for table in _tables(md):
        if not table:
            continue
        hdr = " ".join(table[0].keys())
        if "fact" in hdr and "value" in hdr:                       # Product facts table
            for row in table:
                product_facts.append(ProductFact(fact=_plain(_pick(row, "fact")), value=_plain(_pick(row, "value")),
                                                 source=_page_ref(_pick(row, "source", "page")) if _pick(row, "source", "page") else NOT_STATED))
            continue
        if "field" in hdr and "value" in hdr:                      # Report identity table
            for row in table:
                identity[_plain(_pick(row, "field")).lower()] = _plain(_pick(row, "value"))
            continue
        for row in table:
            name = _plain(_pick(row, "finding", "assessment"))
            cls = _plain(_pick(row, "classification")).lower()
            if not name or not cls:
                continue
            if "root cause" in row or "report records" in row:   # new fixed format
                ref = _pick(row, "clauses", "standard reference")
                page_ref = _page_ref(_pick(row, "report pages", "pages"))
                condition = _plain(_pick(row, "report records")) or NOT_STATED
                comment = _plain(_pick(row, "engineer comment", "comment")) or "—"
                action = _plain(_pick(row, "required action")) or ACTION_BY_CLASS.get(cls, "Review.")
                explicit_rc = _pick(row, "root cause")
                text = f"{condition} {comment}"
            else:                                                   # free format (older answers)
                ref = _pick(row, "standard reference")
                page_ref, text = _pages(_pick(row, "evidence"))
                condition, comment = _split_first(text)
                action = _action(text, cls)
                explicit_rc = ""
            p1, p2 = _clauses(ref)
            refs = ", ".join(x for x in (("P1 " + ", ".join(p1)) if p1 else "", ("P2 " + ", ".join(p2)) if p2 else "") if x)
            if "objective fail" in cls:
                cls = "non-compliant"
                row_verdict = "FAIL / NON-CONFORMITY"
            else:
                row_verdict = VERDICT_BY_CLASS.get(cls, "REVIEW")
            rc = _root_cause(explicit_rc, name, cls, p1, p2)
            if rc == "P0-01" and row_verdict == "INSUFFICIENT DETAIL":
                row_verdict = "HOLD / CORRECT"
            if "identity" in name.lower() and cls != "compliant":
                identity_conflict = True

            critical.append(CriticalFinding(
                root_cause=rc, clause_or_concern=f"{name} ({refs})" if refs else name,
                report_condition=condition, engineer_comments=comment, required_verdict=row_verdict,
                required_action=action, report_reference=page_ref or NOT_STATED))

            if p2:
                risk = ("Critical" if cls == "non-compliant" or rc == "P0-01" else
                        "High" if cls == "missing evidence" and any(int(c.split(".")[-1]) >= 100 for c in p2 if "." in c)
                        else "Medium" if cls == "missing evidence" else "Low")
                coverage.append(CoverageRow(
                    clauses=" / ".join(p2[:6]) + (" …" if len(p2) > 6 else ""), safety_area=name,
                    report_status=cls.capitalize(), applicability_rule="Applicable per supplied Part 2 unless justified",
                    required_outcome=action, risk_level=risk))
            elif p1 and cls in ("non-compliant", "missing evidence"):
                corrections.append(Part1Correction(
                    clause_or_issue=" / ".join(p1[:6]), finding=name, engineer_comments=comment if comment != "—" else condition,
                    required_correction=action, final_status=STATUS_BY_CLASS.get(cls, "REPORT HOLD")))

            for c in p1 + p2:
                is_crit = cls in ("non-compliant", "missing evidence")
                if c in notes:
                    old = notes[c]
                    merged = old.note if name in old.note.split("; ") else f"{old.note}; {name}"
                    notes[c] = ClauseNote(clause=c, critical=old.critical or is_crit, note=merged[:300])
                else:
                    notes[c] = ClauseNote(clause=c, critical=is_crit, note=name)

    # explicit "**Verdict:** ..." line in the fixed format wins
    vm = re.search(r"\*\*Verdict:?\*\*:?\s*(.+)$", md, re.M)
    if vm:
        cand = _plain(vm.group(1))
        for v in get_args(FinalVerdict):
            if cand.upper().startswith(v.split(" — ")[0].upper()) or cand.upper() == v.upper():
                verdict = v
                break
    rm = re.search(r"\*\*Reason:?\*\*:?\s*(.+)$", md, re.M)
    if identity:
        report_meta = {**report_meta,
                       **{k: v for k, v in (("report_number", identity.get("report number")),
                                            ("laboratory", identity.get("laboratory")),
                                            ("report_date", identity.get("report date")),
                                            ("standards_claimed", identity.get("standards claimed"))) if v}}
        report_no = identity.get("report number", report_no)
        product = identity.get("product declared", product)
        declared_model = identity.get("declared model", declared_model)
    reason = _plain(rm.group(1)) if rm else " ".join(re.split(r"(?<=\.)\s", overall)[:2])
    return EvaluationAI(
        title_product=title.title(),
        report_number=report_no,
        laboratory=report_meta.get("laboratory", NOT_STATED),  # identity table / report PDF
        report_date=report_meta.get("report_date", NOT_STATED),
        product_declared=product,
        standards_claimed=report_meta.get("standards_claimed", standards),
        evaluation_basis=limitation or f"Evaluation against {standards}.",
        final_verdict=verdict,
        reason=reason or overall[:400],
        tested_model_matches_declared=not identity_conflict,
        declared_model=declared_model,
        product_facts=product_facts,
        decision_points=decisions,
        final_technical_conclusion=" ".join(x for x in (overall, disposition) if x),
        critical_findings=critical,
        part2_coverage=coverage,
        part1_corrections=corrections,
        clause_notes=list(notes.values()),
    )


def report_meta_from_text(first_pages_text: str) -> dict[str, str]:
    """Best-effort report number / lab / date / standards from the report's first pages (no LLM)."""
    t = first_pages_text
    meta = {}
    m = re.search(r"Report\s*(?:No\.?|Number)[ .:]*\s*([A-Z0-9][A-Z0-9/-]{5,})", t, re.I)
    if m:
        meta["report_number"] = m.group(1)
    m = re.search(r"(?:Testing\s+Laboratory|Prepared\s+By)[ .:]*\s*\n?\s*([A-Z][^\n]{3,80})", t, re.I)
    if m:
        meta["laboratory"] = _plain(m.group(1)).strip(" :.")
    m = re.search(r"Date\s+of\s+(?:Report|issue)[ .:]*\s*\n?\s*([A-Za-z]{3,9}\.?\s?\d{1,2},?\.?\s?\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})", t, re.I)
    if m:
        meta["report_date"] = m.group(1)
    return meta
