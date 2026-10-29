import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="complify-test-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "STORAGE_BACKEND": "local",
    "LOCAL_STORAGE_DIR": f"{_tmp}/storage",
    "STORAGE_BACKEND": "local",
    "OPENAI_API_KEY": "",
    "RATE_LIMIT_ENABLED": "false",
})

STORAGE_DIR = Path(_tmp) / "storage"

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_ai_dep  # noqa: E402
from app.service.ai_service import AIResult  # noqa: E402
from main import app  # noqa: E402


class FakeAI:
    def __init__(self):
        self.asks: list[dict] = []
        self.appended: list[tuple] = []
        self.uploads: list[str] = []
        self.evals: list[dict] = []
        self._n = 0

    def create_conversation(self, metadata):
        self._n += 1
        return f"conv_{self._n}"

    def upload_file(self, filename, data):
        self.uploads.append(filename)
        return f"file_{len(self.uploads)}"

    def ask(self, conversation_id, text, file_ids):
        self.asks.append({"conv": conversation_id, "text": text, "files": list(file_ids)})
        return AIResult(text=f"answer #{len(self.asks)}", model="fake", response_id="r",
                        input_tokens=10, output_tokens=5)

    def evaluate(self, text, file_ids):
        from app.schema.evaluation import (
            ClauseNote, CoverageRow, CriticalFinding, DecisionPoint, EvaluationAI, Part1Correction,
        )
        self.evals.append({"text": text, "files": list(file_ids)})
        ai = EvaluationAI(
            title_product="Test Washing Machine", report_number="TR-001", laboratory="Lab X",
            report_date="1 Jan 2026", product_declared="WM-1, 230 V, 2000 W", standards_claimed="IEC 60335-2-7:2012",
            evaluation_basis="Part 1 + Part 2-7 supplied",
            final_verdict="REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD",
            reason="Rated power conflict.", tested_model_matches_declared=False, declared_model="WM-1",
            decision_points=[DecisionPoint(priority="P0", title="Rating", finding="2000 W vs 400 W (PDF p.1)",
                                           required_resolution="Reconcile ratings.")],
            final_technical_conclusion="Report not acceptable in submitted form.",
            critical_findings=[CriticalFinding(clause_or_concern="10.1 Power input", report_condition="400 W at 230 V",
                engineer_comments="Conflicts with 2000 W cover.", required_verdict="HOLD / CORRECT",
                required_action="Retest.", report_reference="PDF p.1")],
            part2_coverage=[CoverageRow(clauses="15.2", safety_area="Spillage", report_status="P",
                applicability_rule="Applicable", required_outcome="Pass with evidence", risk_level="High")],
            part1_corrections=[Part1Correction(clause_or_issue="Cover", finding="Rating conflict",
                engineer_comments="Not traceable.", required_correction="Correct cover.", final_status="REPORT HOLD")],
            clause_notes=[ClauseNote(clause="10.1", critical=True, note="400 W vs 2000 W cover"),
                          ClauseNote(clause="7.1", critical=False, note="Marking photo missing")],
        )
        return ai, AIResult(text="", model="fake", response_id="r", input_tokens=100, output_tokens=50)

    def append_turn(self, conversation_id, user_text, assistant_text):
        self.appended.append((conversation_id, user_text, assistant_text))


def make_trf_pdf(header: str, rows: list[tuple[str, str, str]]) -> bytes:
    """Report with a TRF-style clause table: clause | requirement | verdict columns."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Helvetica", 9)
    c.drawString(50, 800, header)
    c.drawString(50, 780, "Clause")
    c.drawString(110, 780, "Requirement - Test")
    c.drawString(520, 780, "Verdict")
    y = 760
    for clause, topic, verdict in rows:
        c.drawString(50, y, clause)
        c.drawString(110, y, topic)
        c.drawString(530, y, verdict)
        y -= 16
    c.save()
    return buf.getvalue()


def make_pdf(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in text.splitlines():
        c.drawString(50, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


@pytest.fixture
def fake_ai():
    ai = FakeAI()
    app.dependency_overrides[get_ai_dep] = lambda: ai
    yield ai
    app.dependency_overrides.clear()


@pytest.fixture
def client(fake_ai):
    import shutil

    from app.db.database import Base, engine, init_db

    Base.metadata.drop_all(engine)       # fresh DB + storage per test
    shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    init_db()
    with TestClient(app) as c:
        yield c
