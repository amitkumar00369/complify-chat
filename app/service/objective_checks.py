"""Deterministic (no-AI) objective checks on a TRF test report.

These checks are BINDING: their verdict overrides whatever the AI wrote
(client requirement "post-LLM deterministic re-evaluation").

  P0-01  rating / identity reconciliation   marking vs Clause 7.1 vs Clause 10 table
  P0-03  Clause 10 arithmetic               reported deviation % vs recomputed
  P0-04  Clause 11 heating                  measured rise > stated limit  -> FAIL
  P0-05  Clause 19.10 series-motor overspeed applied voltage < 1.3 x rated -> non-compliant

Line-based scan only (no regex over the whole document), safe on any input.
"""
import re
from dataclasses import dataclass, field

RESOLUTION = {
    "P0-01": "Reconcile declared and tested sample and ratings; re-test or document equivalence.",
    "P0-02": "Supply test evidence or a justified N/A for every applicable Part 2 requirement.",
    "P0-03": "Correct the Clause 10 table and re-issue the calculations at the declared rating.",
    "P0-04": "Resolve the heating exceedance; re-test with the correct limits and configuration.",
    "P0-05": "Repeat abnormal-operation tests under the conditions specified in Clause 19.",
}

ROOT_CAUSES = {
    "P0-01": "Product / sample traceability",
    "P0-02": "Incomplete product-specific (Part 2) evidence",
    "P0-03": "Clause 10 data integrity",
    "P0-04": "Clause 11 heating",
    "P0-05": "Abnormal operation (Clause 19)",
}

_NUM = r"\d+(?:[.,]\d+)?"
_COLS = re.compile(r"\s{2,}")


@dataclass
class ObjectiveFinding:
    root_cause: str                  # "P0-01" ...
    clause: str                      # "10.1"
    title: str
    report_condition: str
    comment: str
    verdict: str                     # one of RequiredVerdict
    action: str
    pages: list[int] = field(default_factory=list)
    objective_fail: bool = False     # True = measured/limit violation (drives REJECT)
    match_values: list[str] = field(default_factory=list)   # numbers that identify the same issue in AI text

    @property
    def page_ref(self) -> str:
        pages = sorted(set(self.pages))
        if not pages:
            return "—"
        return ("PDF p." if len(pages) == 1 else "PDF pp.") + ",".join(map(str, pages))


@dataclass
class ReportFacts:
    declared_power_w: float | None = None
    declared_voltage: str | None = None          # "240" or "220-240"
    declared_voltage_max: float | None = None
    clause71_voltage: str | None = None
    facts_pages: list[int] = field(default_factory=list)


def _f(x: str) -> float:
    return float(x.replace(",", "."))


def _fmt(x: float) -> str:
    return f"{x:g}"


# ------------------------------------------------------------------ facts
def extract_facts(pages: list[str]) -> ReportFacts:
    facts = ReportFacts()
    for no, text in enumerate(pages[:15], start=1):
        for line in text.splitlines()[:400]:
            line = line[:400]
            m = re.search(rf"Ratings?\b[ .:]*:?\s*({_NUM}(?:\s*[-–]\s*{_NUM})?)\s*V\b.*?({_NUM})\s*W\b", line, re.I)
            if m and facts.declared_power_w is None:
                facts.declared_voltage = re.sub(r"\s", "", m.group(1)).replace("–", "-")
                facts.declared_power_w = _f(m.group(2))
                facts.facts_pages.append(no)
                continue
            m = re.search(r"^\s*7\.1\s+Rated voltage[^:]*:\s*(" + _NUM + r"(?:\s*[-–]\s*" + _NUM + r")?)\s*V?\b", line, re.I)
            if m and facts.clause71_voltage is None:
                facts.clause71_voltage = re.sub(r"\s", "", m.group(1)).replace("–", "-")
                facts.facts_pages.append(no)
            m = re.search(rf"Rated power input[^\n]*?\s({_NUM})\s*W\b", line, re.I)
            if m and facts.declared_power_w is None:
                facts.declared_power_w = _f(m.group(1))
                facts.facts_pages.append(no)
    if not facts.declared_voltage and facts.clause71_voltage:
        facts.declared_voltage = facts.clause71_voltage
    if facts.declared_voltage:
        facts.declared_voltage_max = max(_f(v) for v in facts.declared_voltage.split("-") if v)
    return facts


def _in_range(value: str, declared: str) -> bool:
    vals = [_f(v) for v in declared.split("-") if v]
    lo, hi = min(vals), max(vals)
    v = _f(value.split("-")[0])
    return lo * 0.9 <= v <= hi * 1.1


# ------------------------------------------------------------------ checks
def _table_lines(pages: list[str], header: str):
    """Yield (page_no, line) for lines after a 'TABLE: <header>' until the next TABLE / Supplementary."""
    for no, text in enumerate(pages, start=1):
        inside = False
        for line in text.splitlines()[:400]:
            if re.search(r"TABLE\s*:", line, re.I):
                inside = bool(re.search(header, line, re.I))
                continue
            if inside and re.search(r"Supplementary information", line, re.I):
                inside = False
            if inside:
                yield no, line[:400]


def check_clause10(pages: list[str], facts: ReportFacts) -> list[ObjectiveFinding]:
    out: list[ObjectiveFinding] = []
    row = re.compile(rf"^\s*(?P<cond>\S.*?)\s{{2,}}(?P<rated>{_NUM})\s{{1,}}(?P<meas>{_NUM})\s{{2,}}"
                     rf"(?P<dev>[+-]?{_NUM})\s*%\s{{2,}}(?P<req>[+-]?{_NUM})\s*%")
    rows = []
    for no, line in _table_lines(pages, r"Power\s*input\s*deviation"):
        m = row.match(line)
        if m:
            rows.append((no, m))
    if not rows:
        return out
    rated_vals = {_f(m["rated"]) for _, m in rows}
    pages_10 = [no for no, _ in rows]

    # P0-03 arithmetic
    wrong = []
    for no, m in rows:
        rated, meas, dev = _f(m["rated"]), _f(m["meas"]), _f(m["dev"])
        actual = (meas - rated) / rated * 100 if rated else 0
        if abs(actual - dev) > 0.5:
            wrong.append((rated, meas, dev, actual))
    if wrong:
        r, mm, _, actual = wrong[0]
        listed = ", ".join(f"{d:+g}%" for *_, d, _ in wrong)
        out.append(ObjectiveFinding(
            "P0-03", "10.1", "Clause 10 calculation integrity",
            f"Rated {_fmt(r)} W, measured {_fmt(mm)} W; reported deviation {listed}.",
            f"Recomputed deviation is {actual:+.1f}%. Reported percentages do not match the tabulated values.",
            "HOLD / CORRECT", "Correct the Clause 10 table and re-issue the deviation calculation.", pages_10,
            match_values=[_fmt(mm), f"{abs(actual):.1f}%"]))

    # P0-01 rating integrity: Clause 10 basis vs declared marking
    if facts.declared_power_w and all(abs(rv - facts.declared_power_w) / facts.declared_power_w > 0.05 for rv in rated_vals):
        basis = "/".join(_fmt(v) for v in sorted(rated_vals))
        out.append(ObjectiveFinding(
            "P0-01", "10.1", "Rated power: declared marking vs Clause 10 test basis",
            f"Declared rated power input {_fmt(facts.declared_power_w)} W; Clause 10 table uses {basis} W as rated basis.",
            "Test basis does not correspond to the declared product. Downstream test results cannot be attributed "
            "to the declared model until reconciled.",
            "HOLD / CORRECT", "Reconcile rating and tested sample; re-test at the declared rating or document equivalence.",
            sorted(set(pages_10 + facts.facts_pages)), match_values=[basis + " W", basis + "W"]))
    return out


def check_voltage_identity(facts: ReportFacts) -> list[ObjectiveFinding]:
    if facts.clause71_voltage and facts.declared_voltage and facts.clause71_voltage != facts.declared_voltage \
            and not _in_range(facts.clause71_voltage, facts.declared_voltage):
        return [ObjectiveFinding(
            "P0-01", "7.1", "Rated voltage: declared rating vs Clause 7.1 marking",
            f"Declared rating {facts.declared_voltage} V; Clause 7.1 records {facts.clause71_voltage} V.",
            "Marking entry contradicts the declared rating; the report may describe a different sample.",
            "HOLD / CORRECT", "Provide the actual nameplate and align all ratings across the report.",
            facts.facts_pages, match_values=[f"{facts.clause71_voltage} V", f"{facts.clause71_voltage}V"])]
    return []


def check_heating(pages: list[str]) -> list[ObjectiveFinding]:
    fails = []
    for no, line in _table_lines(pages, r"Heating"):
        cells = [c.strip() for c in _COLS.split(line.strip()) if c.strip()]
        if len(cells) < 3 or not re.match(r"[A-Za-z]", cells[0]):
            continue
        nums = []
        for c in cells[1:]:
            m = re.match(rf"^({_NUM})(?:\(|$|\s)", c)
            if m:
                nums.append(_f(m.group(1)))
        if len(nums) >= 4:
            measured, limit = nums[-2], nums[-1]
        elif len(nums) >= 2:
            measured, limit = nums[0], nums[1]
        else:
            continue
        if limit > 0 and measured > limit:
            fails.append((no, cells[0], measured, limit))
    if not fails:
        return []
    desc = "; ".join(f"{loc} {_fmt(m)} K vs {_fmt(l)} K limit" for _, loc, m, l in fails)
    return [ObjectiveFinding(
        "P0-04", "11.8", "Clause 11 heating — temperature rise exceeds stated limit",
        f"Heating table: {desc}.",
        "Measured temperature rise exceeds the limit stated in the report's own table. Objective failure, "
        "subject only to confirming that the table belongs to the tested sample/configuration.",
        "FAIL / NON-CONFORMITY", "Correct the design or re-test; justify any different limit with material/rating evidence.",
        [no for no, *_ in fails], objective_fail=True, match_values=[_fmt(m) for _, _, m, _ in fails])]


def check_19_10(pages: list[str], facts: ReportFacts) -> list[ObjectiveFinding]:
    if not facts.declared_voltage_max:
        return []
    for no, text in enumerate(pages, start=1):
        for line in text.splitlines()[:400]:
            if not re.match(r"^\s*19\.10\s{2,}", line):
                continue
            vm = re.search(r"\s(P|N\s?/\s?A|F)\s*$", line)
            if not vm or vm.group(1).replace(" ", "").upper() != "P":
                return []
            volts = [_f(v) for v in re.findall(rf"({_NUM})\s*V\b", line)]
            if not volts:
                return []
            applied = volts[-1]
            required = round(1.3 * facts.declared_voltage_max, 1)
            if applied < required * 0.99:
                return [ObjectiveFinding(
                    "P0-05", "19.10", "Clause 19.10 series-motor test voltage",
                    f"Recorded test voltage {_fmt(applied)} V, marked P.",
                    f"Clause 19.10 requires 1.3 x rated voltage = {_fmt(required)} V for 1 min at lowest load. "
                    "If no series motor is fitted, the clause applicability must be corrected instead.",
                    "HOLD / CORRECT", f"Repeat 19.10 at {_fmt(required)} V or justify non-applicability.", [no],
                    match_values=[_fmt(applied)])]
            return []
    return []


def run_objective_checks(pages: list[str]) -> tuple[ReportFacts, list[ObjectiveFinding]]:
    facts = extract_facts(pages)
    findings: list[ObjectiveFinding] = []
    for fn in (lambda: check_voltage_identity(facts), lambda: check_clause10(pages, facts),
               lambda: check_heating(pages), lambda: check_19_10(pages, facts)):
        try:
            findings += fn()
        except Exception:  # a check must never break the evaluation
            continue
    return facts, findings


def checks_as_prompt(facts: ReportFacts, findings: list[ObjectiveFinding]) -> str:
    """Text block given to the AI: these results are binding and must not be softened."""
    lines = ["DETERMINISTIC CHECKS (computed by code from the report; binding - do not contradict or soften):"]
    lines.append(f"- Declared rating: {facts.declared_voltage or '?'} V, {_fmt(facts.declared_power_w) if facts.declared_power_w else '?'} W; "
                 f"Clause 7.1 voltage: {facts.clause71_voltage or '?'}")
    for f in findings:
        lines.append(f"- [{f.root_cause}] {f.clause} {f.title}: {f.report_condition} => {f.verdict}")
    if not findings:
        lines.append("- No objective conflicts detected by code.")
    return "\n".join(lines)


# ------------------------------------------------------------------ apply (post-LLM re-evaluation)
def apply_objective_checks(ai, facts: ReportFacts, findings: list[ObjectiveFinding]):
    """Return a copy of EvaluationAI where the code's objective results win over the AI text."""
    from app.schema.evaluation import ClauseNote, CriticalFinding, DecisionPoint, ProductFact

    ai = ai.model_copy(deep=True)
    tag = "[Code-verified] "

    # 1) findings: upgrade the AI row for the same clause, or insert a new row on top
    new_rows = []
    for f in findings:
        row = CriticalFinding(
            root_cause=f.root_cause, clause_or_concern=f"{f.clause} {f.title}",
            report_condition=f.report_condition, engineer_comments=tag + f.comment,
            required_verdict=f.verdict, required_action=f.action, report_reference=f.page_ref)
        new_rows.append(row)
        # AI rows that talk about the same measured values must not contradict the code
        for c in ai.critical_findings:
            if c.required_verdict.startswith("PASS"):      # favourable rows are not the same issue
                continue
            blob = re.sub(r"(\d),(\d{3})", r"\1\2", f"{c.report_condition} {c.engineer_comments}")
            if any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", blob) for v in f.match_values if v):
                if f.objective_fail or c.required_verdict in ("INSUFFICIENT DETAIL", "REVIEW", "CONDITIONAL PASS"):
                    c.required_verdict = f.verdict
                c.root_cause = f.root_cause
                if not c.engineer_comments.startswith(tag):
                    c.engineer_comments = f"{tag}{f.title} -> {f.verdict}. " + c.engineer_comments
    ai.critical_findings = new_rows + ai.critical_findings
    # anything about sample identity is P0-01 traceability
    for c in ai.critical_findings:
        if not c.root_cause and re.search(r"identity|traceab|declared versus tested", c.clause_or_concern, re.I):
            c.root_cause = "P0-01"
            if c.required_verdict in ("INSUFFICIENT DETAIL", "REVIEW"):
                c.required_verdict = "HOLD / CORRECT"

    # 2) clause notes: code findings are always critical
    notes = {n.clause: n for n in ai.clause_notes}
    for f in findings:
        old = notes.get(f.clause)
        text = f"{f.title}: {f.report_condition}"
        notes[f.clause] = ClauseNote(clause=f.clause, critical=True,
                                     note=text if not old else f"{text}; {old.note}"[:300])
    ai.clause_notes = list(notes.values())

    # 3) product facts from code (ratings) if the AI did not give them
    have = {pf.fact.lower() for pf in ai.product_facts}
    fp = sorted(set(facts.facts_pages))
    src = (("PDF p." if len(fp) == 1 else "PDF pp.") + ",".join(map(str, fp))) if fp else "report"
    if facts.declared_voltage and "rated voltage" not in have:
        ai.product_facts.insert(0, ProductFact(fact="Rated voltage", value=f"{facts.declared_voltage} V", source=src))
    if facts.declared_power_w and "rated power" not in have:
        ai.product_facts.insert(1, ProductFact(fact="Rated power", value=f"{_fmt(facts.declared_power_w)} W", source=src))

    # 4) decision points = root causes P0-01..P0-05 first (client format), then the rest
    groups: dict[str, list] = {}
    for c in ai.critical_findings:
        if c.root_cause in ROOT_CAUSES and not c.required_verdict.startswith("PASS"):
            groups.setdefault(c.root_cause, []).append(c)
    def _summary(rc: str, items: list) -> str:
        title = lambda c: re.sub(r"\s*\([^)]*\)\s*$", "", c.clause_or_concern)  # noqa: E731
        lead = items[0]
        text = f"{title(lead)}: {lead.report_condition}"
        if len(items) > 1:
            text += f" Related ({len(items) - 1}): " + "; ".join(title(c) for c in items[1:6])
            text += " …" if len(items) > 6 else ""
        return text[:700]

    gaps = [c for c in ai.part2_coverage if c.risk_level in ("Critical", "High")]
    root_points = [DecisionPoint(priority="P0", title=f"{rc} {ROOT_CAUSES[rc]}", finding=_summary(rc, items),
                                 required_resolution=RESOLUTION[rc])
                   for rc, items in sorted(groups.items())]
    if gaps and "P0-02" not in groups:
        root_points.insert(1 if root_points and root_points[0].title.startswith("P0-01") else 0, DecisionPoint(
            priority="P0", title=f"P0-02 {ROOT_CAUSES['P0-02']}",
            finding=f"{len(gaps)} Part 2 requirement group(s) without adequate evidence: "
                    + ", ".join(g.clauses for g in gaps[:8]),
            required_resolution=RESOLUTION["P0-02"]))
    rest = [d for d in ai.decision_points if not any(w in d.title.lower() for w in
            ("identity", "traceab", "heating", "power", "19.10", "abnormal"))]
    for d in rest:
        if d.priority == "P0":
            d.priority = "P1"
    ai.decision_points = root_points + rest

    # 5) final verdict: objective failure -> REJECT (client manual conclusion wording)
    if any(f.objective_fail for f in findings):
        ai.final_verdict = "REJECT — INSUFFICIENT TECHNICAL EVIDENCE"
        ai.reason = ("Objective failure established by code: "
                     + "; ".join(f.report_condition for f in findings if f.objective_fail) + " " + ai.reason)
    elif any(f.root_cause == "P0-01" for f in findings) and ai.final_verdict.startswith("REPORT ACCEPTED"):
        ai.final_verdict = "REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD"
    if any(f.root_cause == "P0-01" for f in findings):
        ai.tested_model_matches_declared = False
    return ai


# ------------------------------------------------------------------ per-report cache
_CACHE: dict[str, tuple] = {}


async def analyse_report_pdf(data: bytes, sha256: str):
    """(page_texts, facts, findings) for a report PDF, cached in memory by file hash.
    PDF parsing is CPU work -> runs in a worker thread, the event loop stays free."""
    import asyncio

    if sha256 in _CACHE:
        return _CACHE[sha256]
    from app.service.trf_parser import extract_page_texts

    pages = await asyncio.to_thread(extract_page_texts, data)
    facts, findings = await asyncio.to_thread(run_objective_checks, pages)
    if len(_CACHE) > 20:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[sha256] = (pages, facts, findings)
    return _CACHE[sha256]
