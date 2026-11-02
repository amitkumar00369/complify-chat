"""Deterministic (no-AI) objective checks on an IEC 60335 TRF test report.
They run only when the report is against the 60335 series (see applies_to); other standards get none.

Only checks that apply to every appliance under IEC 60335-1 (Part 1 clauses), whatever the product:
  7.1 / 10.1  declared rating vs rating recorded in the tests       -> Inconsistent (Critical)
  10.1        Clause 10 arithmetic: reported deviation % vs recomputed -> Inconsistent (High)
  11.8        measured temperature rise > limit in the report's own table -> Objective fail (Critical)
  19.10       series-motor test voltage < 1.3 x rated (only if the report marks 19.10 as P with a voltage)

Results are BINDING: they override the AI text. Line-based scan only, safe on any input.
"""
import re
from dataclasses import dataclass, field

# what each code check enforces (IEC 60335-1), used as the "normative requirement" of a code row
REQUIREMENT = {
    "7.1": "The rated voltage marked and used for the tests must be the declared rated voltage (IEC 60335-1, 7.1).",
    "10.1": "Power input is measured at rated voltage and compared with the declared rated power input; the deviation "
            "must be within the tolerance of IEC 60335-1, 10.1.",
    "11.8": "Temperature rises must not exceed the limits of the IEC 60335-1, 11.8 table for the part and material.",
    "19.10": "Series motors are supplied at 1.3 times rated voltage for 1 min at the lowest load (IEC 60335-1, 19.10).",
}

_NUM = r"\d+(?:[.,]\d+)?"
_COLS = re.compile(r"\s{2,}")


@dataclass
class ObjectiveFinding:
    clause: str                      # "10.1"
    title: str
    report_condition: str
    comment: str
    status: str                      # "Objective fail" | "Inconsistent"
    severity: str                    # "Critical" | "High"
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

    # arithmetic of the deviation column
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
            "10.1", "Clause 10 calculation integrity",
            f"Rated {_fmt(r)} W, measured {_fmt(mm)} W; reported deviation {listed}.",
            f"Recomputed deviation is {actual:+.1f}%. Reported percentages do not match the tabulated values.",
            "Inconsistent", "High", "Correct the Clause 10 table and re-issue the deviation calculation.", pages_10,
            match_values=[_fmt(mm), f"{abs(actual):.1f}%"]))

    # rating integrity: Clause 10 basis vs declared marking
    if facts.declared_power_w and all(abs(rv - facts.declared_power_w) / facts.declared_power_w > 0.05 for rv in rated_vals):
        basis = "/".join(_fmt(v) for v in sorted(rated_vals))
        out.append(ObjectiveFinding(
            "10.1", "Rated power: declared marking vs Clause 10 test basis",
            f"Declared rated power input {_fmt(facts.declared_power_w)} W; Clause 10 table uses {basis} W as rated basis.",
            "Test basis does not correspond to the declared product. Downstream test results cannot be attributed "
            "to the declared model until reconciled.",
            "Inconsistent", "Critical", "Reconcile rating and tested sample; re-test at the declared rating or document equivalence.",
            sorted(set(pages_10 + facts.facts_pages)), match_values=[basis + " W", basis + "W"]))
    return out


def check_voltage_identity(facts: ReportFacts) -> list[ObjectiveFinding]:
    if facts.clause71_voltage and facts.declared_voltage and facts.clause71_voltage != facts.declared_voltage \
            and not _in_range(facts.clause71_voltage, facts.declared_voltage):
        return [ObjectiveFinding(
            "7.1", "Rated voltage: declared rating vs Clause 7.1 marking",
            f"Declared rating {facts.declared_voltage} V; Clause 7.1 records {facts.clause71_voltage} V.",
            "Marking entry contradicts the declared rating; the report may describe a different sample.",
            "Inconsistent", "Critical", "Provide the actual nameplate and align all ratings across the report.",
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
        "11.8", "Clause 11 heating — temperature rise exceeds stated limit",
        f"Heating table: {desc}.",
        "Measured temperature rise exceeds the limit stated in the report's own table. Objective failure, "
        "subject only to confirming that the table belongs to the tested sample/configuration.",
        "Objective fail", "Critical", "Correct the design or re-test; justify any different limit with material/rating evidence.",
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
                    "19.10", "Clause 19.10 series-motor test voltage",
                    f"Recorded test voltage {_fmt(applied)} V, marked P.",
                    f"Clause 19.10 requires 1.3 x rated voltage = {_fmt(required)} V for 1 min at lowest load. "
                    "If no series motor is fitted, the clause applicability must be corrected instead.",
                    "Non-compliant", "High", f"Repeat 19.10 at {_fmt(required)} V or justify non-applicability.", [no],
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
        lines.append(f"- {f.clause} {f.title}: {f.report_condition} => {f.status} ({f.severity})")
    if not findings:
        lines.append("- No objective conflicts detected by code.")
    return "\n".join(lines)


# ------------------------------------------------------------------ apply (post-LLM re-evaluation)
_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Not stated": 4}


def apply_objective_checks(ai, facts: ReportFacts, findings: list[ObjectiveFinding]):
    """Return a copy of EvaluationAI where the code's objective results win over the AI text."""
    from app.schema.evaluation import Finding

    ai = ai.model_copy(deep=True)

    # 1) code results confirm the AI row that reports the same values (no duplicate rows);
    #    only an issue the AI missed is added as its own row
    for f in findings:
        same = []
        for row in ai.findings:
            blob = re.sub(r"(\d),(\d{3})", r"\1\2", f"{row.finding} {row.reason}")
            if any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", blob) for v in f.match_values if v):
                same.append(row)
        for row in same:
            row.code_verified = True
            if _RANK.get(f.severity, 4) < _RANK.get(row.severity, 4):
                row.severity = f.severity
        if not same:
            gap = "LIMIT_EXCEEDED" if f.status == "Objective fail" else (
                "PRODUCT_IDENTITY_CONFLICT" if f.severity == "Critical" else "NUMERIC_MISMATCH"
                if f.status == "Inconsistent" else "INCORRECT_TEST_CONDITION")
            ai.findings.append(Finding(clause=f.clause, gap_type=gap,
                                       finding=f"{f.title}: {f.report_condition} {f.comment}",
                                       reason=REQUIREMENT.get(f.clause, f.title),
                                       required_action=f.action, pages=f.page_ref, severity=f.severity,
                                       report_quote="", standard_quote="not in supplied standards",
                                       code_verified=True, report_check="checked by code",
                                       standard_check="rule in code (IEC 60335-1)"))
    ai.findings.sort(key=lambda r: _RANK.get(r.severity, 4))       # stable: AI order kept within a level

    # 2) verdict: a measured failure -> REJECT; a rating conflict cannot be "accepted"
    rating_conflict = any(f.severity == "Critical" and f.status == "Inconsistent" for f in findings)
    if any(f.status == "Objective fail" for f in findings):
        ai.final_verdict = "REJECT — INSUFFICIENT TECHNICAL EVIDENCE"
        ai.reason = ("Objective failure established by code: "
                     + "; ".join(f.report_condition for f in findings if f.status == "Objective fail") + " " + ai.reason)
    elif rating_conflict and ai.final_verdict.startswith("REPORT ACCEPTED"):
        ai.final_verdict = "REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD"
    if rating_conflict:
        ai.tested_model_matches_declared = False
    return ai


def applies_to(pages: list[str]) -> bool:
    """The rules above are IEC/EN 60335-1 clauses: run them only on reports against that series."""
    head = " ".join(pages[:6])
    return bool(re.search(r"\b(?:IEC|EN|AS/NZS|BS EN|GSO IEC|SASO IEC)?\s*60335\b", head))


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
    if applies_to(pages):
        facts, findings = await asyncio.to_thread(run_objective_checks, pages)
    else:                      # other standard families: these IEC 60335-1 rules do not apply
        facts, findings = ReportFacts(), []
    if len(_CACHE) > 20:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[sha256] = (pages, facts, findings)
    return _CACHE[sha256]