"""Chat answer (markdown) -> EvaluationAI, with NO LLM call.

Generic: nothing here depends on the product type or the standard. Every value comes from the
AI answer; when the answer does not state something the field says "not stated", the code never
writes its own wording into a finding.

Supported answer formats (findings table)
  current   | Clause | Gap type | Priority | Normative requirement | Finding reason | Required action | Pages |
            | Report quote | Standard quote |
  previous  | Clause | Finding | Reason | Required action | Pages | Severity | Report quote | Standard quote |
  previous  | Clause | Concern | Severity | Status | Report value | Issue | Required action | Pages |
  older     | ID | Finding | Root cause | Classification | Severity | Clauses | Report pages | Report records | ...
  oldest    | Finding | Classification | Standard reference | Evidence |
Rows classified Compliant / Not applicable in older answers are not problems and are skipped.
"""
import re
from typing import get_args

from app.schema.evaluation import ClauseNote, EvaluationAI, Finding, FinalVerdict, GapType

NOT_STATED = "not stated"

_SEVERITY = {s.lower(): s for s in ("Critical", "High", "Medium", "Low")}
_OLD_SEVERITY = {"p0": "Critical", "p1": "High", "p2": "Medium"}      # older answers used P0/P1/P2
_IMPERATIVE = re.compile(
    r"(?:^|(?<=[.;]\s))((?:Provide|Confirm|Resolve|Determine|Identify|Establish|Supply|Correct|"
    r"Apply|Verify|Address|Reconcile|Repeat|Retain|Obtain|Replace|Re-test|Clarify)\b(?:[^.]|\.(?=\d))*\.)")
_CLAUSE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){0,4})(?:\s*[–-]\s*(\d{1,3}(?:\.\d{1,3}){0,4}))?(?![\w.])")
_PG = r"\d+(?![.\d])(?:\s*[–-]\s*\d+(?![.\d]))?"
_PAGE_LIST = re.compile(rf"[Pp]ages?\s+({_PG}(?:\s*(?:,|and)\s*{_PG})*)")


# ------------------------------------------------------------------ helpers
def _plain(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text or "")
    text = text.replace("“", '"').replace("”", '"').replace("<br>", " ")
    return re.sub(r"\s+", " ", text).strip()


def _sections(md: str) -> dict[str, str]:
    out, current, buf = {}, "_top", []
    for line in md.splitlines():
        m = re.match(r"^#{1,4}\s+(.*)$", line)
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
    for key in keys:                      # key order = priority
        for k in row:
            if key in k:
                return row[k]
    return ""


def _page_ref(cell: str) -> str:
    """'77, 79' / 'PDF pp.77,79' / 'Pages 13–15 and 77' -> 'PDF pp.77,79'."""
    cell = _plain(cell)
    if not cell or cell in ("-", "—"):
        return NOT_STATED
    if cell.upper().startswith("PDF"):
        return cell
    nums = [re.sub(r"\s", "", n).replace("-", "–") for n in re.findall(_PG, cell)]
    if not nums:
        return cell
    return ("PDF p." if len(nums) == 1 and "–" not in nums[0] else "PDF pp.") + ",".join(nums)


def _pages_from_evidence(evidence: str) -> tuple[str, str]:
    """oldest format: '**Pages 2, 4 and 77:** text' -> ('PDF pp.2,4,77', 'text')."""
    m = re.match(r"\s*\*\*(Pages?\s+[^*]+?):?\*\*:?\s*(.*)$", evidence, re.S)
    if not m:
        return NOT_STATED, _plain(evidence)
    nums: list[str] = []
    for lst in _PAGE_LIST.findall(m.group(1)):
        nums += [re.sub(r"\s", "", n).replace("-", "–") for n in re.findall(_PG, lst)]
    if not nums:
        return NOT_STATED, _plain(evidence)
    return ("PDF p." if len(nums) == 1 and "–" not in nums[0] else "PDF pp.") + ",".join(nums), _plain(m.group(2))


def _clauses(ref: str) -> list[str]:
    """'P1 10.1, Table 1; P2 10.1, 13.1–13.3' -> ['10.1', '13.1', '13.2', '13.3']."""
    out: list[str] = []
    for chunk in re.split(r"[;,]", _plain(ref)):
        chunk = re.sub(r"\b(Tables?|Annex|Figure)\s+[A-Z0-9]+(?:\s*[–-]\s*[A-Z0-9]+)?", "", chunk)
        chunk = re.sub(r"\bP[12]\b", "", chunk)
        for a, b in _CLAUSE.findall(chunk):
            found = [a]
            if b:
                ha = a.rsplit(".", 1) if "." in a else ("", a)
                hb = b.rsplit(".", 1) if "." in b else ("", b)
                if ha[0] == hb[0] and ha[1].isdigit() and hb[1].isdigit() and 0 < int(hb[1]) - int(ha[1]) <= 20:
                    found = [f"{ha[0]}.{i}" if ha[0] else str(i) for i in range(int(ha[1]), int(hb[1]) + 1)]
                else:
                    found = [a, b]
            out.extend(found)
    return list(dict.fromkeys(out))


def _not_a_problem(status_cell: str) -> bool:
    return _plain(status_cell).lower().startswith(("compliant", "not applicable", "pass"))


def _severity(text: str) -> str:
    t = _plain(text).lower()
    return _SEVERITY.get(t) or _OLD_SEVERITY.get(t[:2]) or "Not stated"


def _first(pattern: str, text: str, default: str = NOT_STATED) -> str:
    m = re.search(pattern, text, re.I | re.M)
    return _plain(m.group(1)) if m else default


def _join(a: str, b: str) -> str:
    a, b = _plain(a), _plain(b)
    return f"{a}: {b}" if a and b else (a or b)


_GAP_TYPES = set(get_args(GapType))


def _gap_type(cell: str) -> str:
    t = re.sub(r"[^A-Z]+", "_", _plain(cell).upper()).strip("_")
    if not t:
        return "NOT_STATED"
    return t if t in _GAP_TYPES else "OTHER"


def _row_to_finding(row: dict[str, str]) -> Finding | None:
    gap_type = _gap_type(_pick(row, "gap type")) if "gap type" in row else "NOT_STATED"
    if "gap type" in row or "normative requirement" in row:         # current format
        clause_cell, status_cell = _pick(row, "clause"), ""
        finding, reason = _plain(_pick(row, "finding reason", "finding")), _plain(_pick(row, "normative requirement"))
        action, pages = _plain(_pick(row, "required action")), _page_ref(_pick(row, "pages"))
        severity = _severity(_pick(row, "priority", "severity"))
    elif "reason" in row:                                           # previous format (Finding | Reason)
        clause_cell, status_cell = _pick(row, "clause"), ""
        finding, reason = _plain(_pick(row, "finding")), _plain(_pick(row, "reason"))
        action, pages = _plain(_pick(row, "required action")), _page_ref(_pick(row, "pages"))
        severity = _severity(_pick(row, "severity"))
    elif "concern" in row:                                          # previous format
        clause_cell, status_cell = _pick(row, "clause"), _pick(row, "status")
        finding = _join(_pick(row, "concern"), _pick(row, "report value"))
        reason, action = _plain(_pick(row, "issue")), _plain(_pick(row, "required action"))
        pages, severity = _page_ref(_pick(row, "pages")), _severity(_pick(row, "severity"))
    elif "report records" in row or "root cause" in row:           # older fixed format
        clause_cell, status_cell = _pick(row, "clauses"), _pick(row, "classification")
        finding = _join(_pick(row, "finding"), _pick(row, "report records"))
        reason, action = _plain(_pick(row, "engineer comment", "comment")), _plain(_pick(row, "required action"))
        pages, severity = _page_ref(_pick(row, "report pages", "pages")), _severity(_pick(row, "severity"))
    else:                                                           # oldest free format
        name, status_cell = _pick(row, "finding", "assessment"), _pick(row, "classification")
        if not name or not status_cell:
            return None
        clause_cell = _pick(row, "standard reference", "clause")
        pages, evidence = _pages_from_evidence(_pick(row, "evidence"))
        finding, reason = _join(name, evidence), ""
        action, severity = " ".join(_IMPERATIVE.findall(evidence)), "Not stated"
    if not finding or _not_a_problem(status_cell):
        return None
    clauses = _clauses(clause_cell)
    return Finding(clause="; ".join(clauses) or "—", gap_type=gap_type, finding=finding, reason=reason or NOT_STATED,
                   required_action=action or NOT_STATED, pages=pages, severity=severity,
                   report_quote=_quote(_pick(row, "report quote", "report evidence")),
                   standard_quote=_quote(_pick(row, "standard quote", "standard requirement")),
                   code_verified=False, report_check="not checked", standard_check="not checked")


def _quote(cell: str) -> str:
    q = _plain(cell).strip().strip('"\'').strip()
    return q if q and q not in ("-", "—") else "none"


def clause_notes(findings: list[Finding]) -> list[ClauseNote]:
    """Per-clause note for the clause register: first words of every finding on that clause."""
    notes: dict[str, ClauseNote] = {}
    for f in findings:
        short = f.finding.split(":")[0][:120]
        for c in f.clause.split("; "):
            if c == "—":
                continue
            old = notes.get(c)
            text = short if not old or short in old.note else f"{old.note}; {short}"
            notes[c] = ClauseNote(clause=c, critical=True, note=text[:300])
    return list(notes.values())


# ------------------------------------------------------------------ main
def parse_chat_markdown(md: str, report_meta: dict[str, str] | None = None) -> EvaluationAI:
    meta = dict(report_meta or {})
    sec = _sections(md)
    find = lambda *keys: next((v for k, v in sec.items() if any(key in k for key in keys)), "")  # noqa: E731

    identity: dict[str, str] = {}
    findings: list[Finding] = []
    for table in _tables(md):
        if not table:
            continue
        hdr = " ".join(table[0].keys())
        if "field" in hdr and "value" in hdr:
            for row in table:
                identity[_plain(_pick(row, "field")).lower()] = _plain(_pick(row, "value"))
        elif "fact" in hdr and "value" in hdr:
            continue                                   # product facts (older answers) are not part of the output
        else:
            findings += [f for f in (_row_to_finding(r) for r in table) if f]

    product = identity.get("product declared") or _first(r"\*\*Product declared:\*\*\s*(.+)$", md)
    report_no = identity.get("report number") or _first(r"\*\*Report:\*\*\s*([^\s—-]+)", md,
                                                        meta.get("report_number", NOT_STATED))
    declared_model = identity.get("declared model") or _first(r"model\s+([A-Z0-9][\w/.-]+)", product).rstrip(",.")
    std_evaluated = identity.get("standards evaluated") or _first(r"^-\s*Standards:\s*(.+)$", md)

    # title: product type (+ brand when the declaration names one in capitals), from the answer only
    bits = [b.strip() for b in product.split(",") if b.strip()] if product != NOT_STATED else []
    brand = next((b for b in bits[1:3] if re.fullmatch(r"[A-Z][A-Z0-9&.-]{2,19}", b)), "")
    title = f"{brand} {bits[0]}".strip() if bits else "Test report"

    overall = _plain(find("overall conclusion"))

    verdict = None
    vm = re.search(r"\*\*Verdict:?\*\*:?\s*(.+)$", md, re.M)
    if vm:
        cand = _plain(vm.group(1)).upper()
        verdict = next((v for v in get_args(FinalVerdict)
                        if cand == v.upper() or cand.startswith(v.split(" — ")[0].upper())), None)
    if verdict is None:            # oldest answers had no verdict line: read the AI's own conclusion words
        low = (overall + " " + _plain(find("required disposition"))).lower()
        if "non-conform" in low and "established" in low:
            verdict = "REPORT REJECTED — NON-CONFORMITY ESTABLISHED"
        elif "accept" in low and "minor" in low:
            verdict = "REPORT ACCEPTED SUBJECT TO MINOR CORRECTIONS"
        elif re.search(r"demonstrates? compliance|compliance (is )?demonstrated", low) and not findings:
            verdict = "REPORT ACCEPTED — PRODUCT COMPLIANCE DEMONSTRATED"
        else:
            verdict = "REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD"
    am = re.search(r"\*\*Recommended action:?\*\*:?\s*(.+)$", md, re.M)
    rm = re.search(r"\*\*Reason:?\*\*:?\s*(.+)$", md, re.M)
    reason = _plain(rm.group(1)) if rm else " ".join(re.split(r"(?<=\.)\s", overall)[:2])

    identity_conflict = any(
        re.search(r"identity|traceab|declared (versus|vs\.?) tested|tested sample", f.finding.split(":")[0], re.I)
        for f in findings)
    return EvaluationAI(
        title_product=title.upper(),
        report_number=report_no,
        laboratory=identity.get("laboratory") or meta.get("laboratory", NOT_STATED),
        report_date=identity.get("report date") or meta.get("report_date", NOT_STATED),
        product_declared=product,
        declared_model=declared_model or NOT_STATED,
        standards_claimed=identity.get("standards claimed") or meta.get("standards_claimed", NOT_STATED),
        standards_evaluated=std_evaluated,
        final_verdict=verdict,
        reason=reason or NOT_STATED,
        recommended_action=_plain(am.group(1)) if am else NOT_STATED,
        conclusion=overall or NOT_STATED,
        tested_model_matches_declared=not identity_conflict,
        findings=findings,
    )


def report_meta_from_text(first_pages_text: str) -> dict[str, str]:
    """Best-effort report number / lab / date from the report's first pages (no LLM)."""
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