"""Clause registers of the supplied standards, read by code from the standard PDFs (no AI).

Part 1: every numbered clause heading from clause 5 on (clause 5 sub-clauses are general test conditions,
        not requirements, so only "5" itself is kept); "Void" clauses are skipped.
Part 2: see below.

A Part 2 (particular requirements) says, clause by clause, how it changes Part 1:
    "11 Heating / This clause of Part 1 is applicable except as follows."
    "11.7 Replacement: ..."   "15.3 Addition: ..."   "19.9 Not applicable."
    "22.101 Pumps shall withstand ..."          (new clause, number >= 101)
This module lists every clause the Part 2 adds, modifies, replaces or declares not applicable.
The list is the same for any product family, so the AI must give a disposition for each one
instead of choosing what to look at.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from app.service.trf_parser import extract_page_texts

FIRST_TESTED_CLAUSE = 5          # 1-4 are scope, references, definitions, general requirement
MAX_TOP = 32

_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,4})\s{1,}(\S.*)$")
_KEYWORD = re.compile(r"^(Addition|Modification|Replacement|Not applicable|Void)\b", re.I)


@dataclass
class StandardClause:
    clause: str
    change: str          # addition | modification | replacement | new | not applicable | applicable with changes
    text: str            # first words of the requirement (topic hint for the AI)

    def as_dict(self) -> dict:
        return asdict(self)


def _key(c: str) -> tuple[int, ...]:
    return tuple(int(x) for x in c.split("."))


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _noise(line: str) -> bool:
    low = line.lower()
    return ("licensed to" in low or "licensed by" in low or "licence" in low or "downloaded" in low
            or "networking prohibited" in low or "eshop" in low or "not for commercial use" in low or "© iec" in low
            or bool(re.search(r"\.{5,}", line)) or bool(re.fullmatch(r"\s*[–-]\s*\d+\s*[–-]\s*", line)))


def _headings(page_texts: list[str]) -> tuple[list[str], list[tuple[int, str, str]]]:
    lines = [l for t in page_texts for l in t.splitlines() if l.strip() and not _noise(l)]
    # body starts at the last "1 Scope" heading (the first ones are the table of contents)
    start = max((i for i, l in enumerate(lines) if re.match(r"^\s*1\s+Scope\b", l)), default=0)
    heads: list[tuple[int, str, str]] = []
    last: tuple[int, ...] = (0,)
    for i in range(start, len(lines)):
        m = _HEAD.match(lines[i])
        if not m:
            continue
        num, rest = m.group(1), _clean(m.group(2))
        k = _key(num)
        if not (1 <= k[0] <= MAX_TOP) or k <= last:
            continue                                   # clauses only move forward; skips list items, values
        if not (rest[:1].isupper() or _KEYWORD.match(rest)):
            continue
        if k[0] > last[0] + 1 and len(k) == 1 and last != (0,):
            continue                                   # a jump of several top clauses = not a heading
        heads.append((i, num, rest))
        last = k
    return lines, heads


def parse_part1_clauses(page_texts: list[str]) -> list[StandardClause]:
    _, heads = _headings(page_texts)
    out = []
    for _i, num, rest in heads:
        k = _key(num)
        if k[0] < FIRST_TESTED_CLAUSE or (k[0] == FIRST_TESTED_CLAUSE and len(k) > 1):
            continue
        if re.match(r"(Void|Vacant)\b", rest, re.I) or re.match(r"Annex\b", rest):
            continue
        out.append(StandardClause(clause=num, change="part 1 requirement", text=rest[:180]))
    return out


def parse_part2_clauses(page_texts: list[str]) -> list[StandardClause]:
    lines, heads = _headings(page_texts)
    out: list[StandardClause] = []
    for n, (i, num, rest) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else min(len(lines), i + 40)
        body = _clean(" ".join(lines[i + 1:end]))
        k = _key(num)
        if k[0] < FIRST_TESTED_CLAUSE or re.match(r"Annex\b", rest):
            continue
        kw = _KEYWORD.match(rest)
        low = body.lower()
        if kw:
            change = kw.group(1).lower()
            text = _clean(rest[kw.end():].lstrip(":. ")) or body or kw.group(1)
        elif len(k) == 1:
            if "is not applicable" in low[:120]:
                change = "not applicable"
            elif "except as follows" in low[:160]:
                change = "applicable with changes"
            elif "is replaced by" in low[:160]:
                change = "replacement"
            else:
                continue                               # "This clause of Part 1 is applicable." -> no change
            text = rest
        elif k[-1] >= 101:
            change, text = "new", rest + " " + body
        else:
            continue
        if change == "void":
            continue
        out.append(StandardClause(clause=num, change=change, text=_clean(text)[:180]))
    return out


_CACHE: dict[tuple[str, str], list[StandardClause]] = {}


def part2_clauses_from_pdf(data: bytes) -> list[StandardClause]:
    return clauses_from_pdf(data, "part2")


def clauses_from_pdf(data: bytes, part_type: str) -> list[StandardClause]:
    key = (hashlib.sha256(data).hexdigest(), part_type)
    if key not in _CACHE:
        texts = extract_page_texts(data)
        _CACHE[key] = parse_part2_clauses(texts) if part_type == "part2" else parse_part1_clauses(texts)
    return _CACHE[key]


Register = tuple[str, str, list[StandardClause]]          # (standard label, "Part 1" | "Part 2", clauses)


async def standard_registers(storage, standards, parts: tuple[str, ...] = ("part2",)) -> list[Register]:
    """Clause registers of the selected standards (PDF parsing in a worker thread).
    Default: Part 2 only (that is what goes into the AI prompt)."""
    import asyncio

    from app.utils.pdf_utils import format_ref

    out: list[Register] = []
    for s in standards:
        part = {"part1": "Part 1", "part2": "Part 2"}.get(s.part_type)
        if not part or s.part_type not in parts:
            continue
        try:
            data = await storage.get(s.storage_key)
            clauses = await asyncio.to_thread(clauses_from_pdf, data, s.part_type)
        except Exception:      # a missing/odd standard file must not block the evaluation
            continue
        if clauses:
            out.append((format_ref(s.name, s.edition), part, clauses))
    return out


_NOT_FOUND = ("This {std} clause is not in the report's clause table, so no result or N/A is recorded.",
              "Add the test result, or a justified N/A, for this clause.")
_REPORTED_NA = ("The report marks this {std} clause N/A without stating why it does not apply.",
                "State why the clause does not apply to this product, or provide the test result.")


def _pages(pages: list[int]) -> str:
    if not pages:
        return "—"
    return ("PDF p." if len(pages) == 1 else "PDF pp.") + ",".join(map(str, pages))


def report_gaps(registers: list[Register], rows) -> list[dict]:
    """Clauses of the supplied standards that the report does not cover (Part 1 + Part 2),
    or reports as N/A (Part 2 only: Part 1 N/A rows are normal and are reviewed by the AI)."""
    by_clause = {}
    for r in rows:
        by_clause.setdefault(r.clause, r)
    part2 = [c for _l, p, cl in registers if p == "Part 2" for c in cl]
    p2_listed = {c.clause for c in part2}
    p2_not_applicable = {c.clause for c in part2 if c.change == "not applicable"}

    def _excluded_by_part2(clause: str) -> bool:      # clause, or a parent clause, declared N/A by Part 2
        parts = clause.split(".")
        return any(".".join(parts[:i]) in p2_not_applicable for i in range(1, len(parts) + 1))

    gaps = []
    for label, part, clauses in registers:
        std = f"{part} ({label})"
        for sc in clauses:
            if sc.change == "not applicable" or _excluded_by_part2(sc.clause):
                continue
            if part == "Part 1" and sc.clause in p2_listed:
                continue                                  # handled with the Part 2 wording
            r = by_clause.get(sc.clause)
            if r is None:
                reason, action = _NOT_FOUND
                status, pages = "Not in report", []
            elif part == "Part 2" and (r.verdict or "").upper().replace(" ", "") in ("N/A", "NA"):
                reason, action = _REPORTED_NA
                status, pages = "Reported N/A", list(r.pages)
            else:
                continue
            gaps.append({"part": part, "clause": sc.clause, "status": status, "requirement": sc.text,
                         "reason": reason.format(std=std), "required_action": action, "pages": _pages(pages)})
    return sorted(gaps, key=lambda g: (g["part"] != "Part 2", _key(g["clause"])))


def registers_as_prompt(registers: list[Register], rows) -> str:
    """Part 2 register + where each clause is in the report (so the AI checks every one).
    Part 1 is not listed (too long; its gaps are computed by code)."""
    by_clause = {}
    for r in rows:
        by_clause.setdefault(r.clause, r)
    lines = []
    for label, part, clauses in registers:
        if part != "Part 2":
            continue
        lines += [f"CLAUSE REGISTER of {label} (particular part; read by code from the standard). "
                  "clause | change to the general part | requirement (first words) | report verdict | report pages:"]
        for c in clauses:
            r = by_clause.get(c.clause)
            where = f"{r.verdict or '-'} | {','.join(map(str, r.pages))}" if r else "NOT IN REPORT | -"
            lines.append(f"{c.clause} | {c.change} | {c.text[:120]} | {where}")
    return "\n".join(lines)