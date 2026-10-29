"""Deterministic extraction of the clause table from an IEC TRF test report.

TRF pages look like:
    Clause   Requirement - Test                     Result - Remark     Verdict
    5.7      Temperature of liquid maintained ...                       P
             5 °C of temperature marked on pump
    6.1      Protection against electric shock:     Class I             P

Linear, line-based scan (no regex over the whole document -> no ReDoS),
with hard caps on pages / lines / line length.
"""
import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
logging.getLogger("pdfminer").setLevel(logging.ERROR)

MAX_PAGES = 400
MAX_LINES_PER_PAGE = 400
MAX_LINE_LEN = 500
MAX_TOPIC_LEN = 160

_CLAUSE_START = re.compile(r"^\s{0,24}(\d{1,3}(?:\.\d{1,3}){0,4})\s{2,}(\S.*)$")
_VERDICT_END = re.compile(r"\s{2,}(P|N\s?/\s?A|N\.A\.?|F|Pass|Fail|PASS|FAIL)\s*$")
_SPLIT_COLS = re.compile(r"\s{6,}")   # column gap (words inside a cell use < 6 spaces)


@dataclass
class ClauseRow:
    clause: str
    topic: str
    verdict: str            # P | N/A | F | "" (not stated)
    pages: list[int] = field(default_factory=list)
    remark: str = ""

    @property
    def top(self) -> int:
        return int(self.clause.split(".")[0])

    @property
    def is_parent(self) -> bool:
        return "." not in self.clause

    @property
    def page_ref(self) -> str:
        if not self.pages:
            return ""
        shown = ",".join(str(p) for p in self.pages[:4]) + ("…" if len(self.pages) > 4 else "")
        return ("PDF p." if len(self.pages) == 1 else "PDF pp.") + shown


def _norm_verdict(v: str) -> str:
    v = v.upper().replace(" ", "")
    if v in ("P", "PASS"):
        return "P"
    if v in ("F", "FAIL"):
        return "F"
    return "N/A"


def _clean(text: str) -> str:
    text = re.sub(r"\.{4,}\s*:?", " ", text)          # dot leaders "......:"
    return re.sub(r"\s+", " ", text).strip(" :")


def _plausible(clause: str, rest: str) -> bool:
    parts = [int(p) for p in clause.split(".")]
    if not 1 <= parts[0] <= 40 and not 100 <= parts[0] <= 199:
        return False
    if any(p > 300 for p in parts[1:]):
        return False
    if len(parts) == 1:
        # top-level rows are headings: "CLASSIFICATION", "MARKING AND INSTRUCTIONS"
        letters = [c for c in rest.split("  ")[0] if c.isalpha()]
        return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8
    return True


def extract_page_texts(data: bytes, max_pages: int = MAX_PAGES) -> list[str]:
    import pdfplumber

    texts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            try:
                texts.append(page.extract_text(layout=True, x_tolerance=1, x_density=5, y_density=13) or "")
            except Exception as exc:  # one bad page must not kill the run
                logger.warning("page %s extraction failed: %s", page.page_number, exc)
                texts.append("")
    return texts


def parse_clause_rows(page_texts: list[str]) -> list[ClauseRow]:
    rows: dict[str, ClauseRow] = {}
    order: list[str] = []
    current: ClauseRow | None = None
    max_top = 0          # annexes restart numbering (5.2, 6.3 ...) -> ignore new low clauses late in the report

    for page_no, text in enumerate(page_texts, start=1):
        for raw in text.splitlines()[:MAX_LINES_PER_PAGE]:
            line = raw.rstrip()[:MAX_LINE_LEN]
            if not line.strip():
                continue
            vm = _VERDICT_END.search(line)
            body = line[: vm.start()] if vm else line
            m = _CLAUSE_START.match(body if vm else line)

            if m and _plausible(m.group(1), m.group(2)):
                clause = m.group(1)
                cols = _SPLIT_COLS.split(m.group(2).strip())
                topic = _clean(cols[0])[:MAX_TOPIC_LEN]
                remark = _clean(" ".join(cols[1:]))[:MAX_TOPIC_LEN] if len(cols) > 1 else ""
                remark = "" if remark in ("/", "-", "--") else remark
                verdict = _norm_verdict(vm.group(1)) if vm else ""
                if clause in rows:                       # appendix tables repeat clauses
                    row = rows[clause]
                    if page_no not in row.pages:
                        row.pages.append(page_no)
                    if not row.verdict and verdict:
                        row.verdict = verdict
                    current = row
                elif int(clause.split(".")[0]) < max_top - 10 or topic in ("", "--", "-"):
                    current = None
                    continue
                else:
                    current = ClauseRow(clause, topic, verdict, [page_no], remark)
                    max_top = max(max_top, int(clause.split(".")[0]))
                    rows[clause] = current
                    order.append(clause)
                continue

            # continuation line: first verdict found below a clause start belongs to it
            if current and not current.verdict and vm:
                current.verdict = _norm_verdict(vm.group(1))
    return [rows[c] for c in order]


def extract_clause_rows(data: bytes) -> tuple[list[ClauseRow], int]:
    texts = extract_page_texts(data)
    return parse_clause_rows(texts), len(texts)
