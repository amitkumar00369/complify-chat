"""POST /engine-evaluations: effective_kb.json + report_evidence.json -> PDF / XLSX / JSON / MD."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

from app.api.deps import get_ai_dep
from main import app

API = "/api/v1"
DATA = Path(__file__).parent / "data" / "engine"
MD = """## Report identity
| Field | Value |
|---|---|
| Report number | STUESO0520024332206LM |
| Laboratory | Guangzhou STU Hengchuang Testing Service Co. Ltd |
| Report date | 17 April 2026 |
| Product declared | Washing machine |
| Declared model | EC/27FW |
| Standards claimed | IEC 60335-1:2010; IEC 60335-2-7:2012 |
| Standards evaluated | IEC 60335-1:2010; IEC 60335-2-7:2012 |

## Findings
| Clause | Gap type | Priority | Normative requirement | Finding reason | Required action | Pages | Report quote | Standard quote |
|---|---|---|---|---|---|---|---|---|
| 10.1 | NUMERIC_MISMATCH | HIGH | Power input within Table 1 deviation. | Table uses 2500 W rated. | Retest. | 77 | 240V, 50Hz 2500 2580 | the power input at normal operating temperature shall not deviate |
| 22.101 | CLAUSE_NOT_ADDRESSED | HIGH | Door interlock. | No row. | Add test. | — | none | it shall not be possible to open the door by a simple action |

## Final decision
**Verdict:** REPORT CORRECTION / REPLACEMENT REQUIRED — PRODUCT COMPLIANCE NOT DETERMINED — CERTIFICATION HOLD
**Reason:** Gaps.
**Recommended action:** Correct the report.

## Overall conclusion
Not acceptable as submitted.
"""


class FakeClient:
    def __init__(self):
        self.calls = []
        self.responses = NS(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return NS(output_text=MD, model="gpt-6.1-terra", status="completed", incomplete_details=None,
                  usage=NS(input_tokens=1200, output_tokens=300, input_tokens_details=NS(cached_tokens=0)))


def _files():
    return {"effective_kb": ("effective_kb.json", (DATA / "effective_kb.json").read_bytes(), "application/json"),
            "report_evidence": ("report_evidence.json", (DATA / "report_evidence.json").read_bytes(),
                                "application/json")}


def test_engine_evaluation_end_to_end(client):
    fake = FakeClient()
    app.dependency_overrides[get_ai_dep] = lambda: NS(client=fake)
    try:
        r = client.post(f"{API}/engine-evaluations", files=_files())
        assert r.status_code == 200, r.text
        body = r.json()
        sent = fake.calls[0]["input"][0]["content"]
        assert sent[0]["text"].startswith("# STANDARD") and sent[1]["text"].startswith("# REPORT")
        assert set(body) == {"evaluation_id", "from_cache", "report_filename", "standards_used", "final_verdict",
                             "result", "download_pdf", "download_json", "download_md", "download_xlsx"}
        res = body["result"]                                  # same gap JSON structure as /evaluations
        assert set(res) == {"report_id", "gaps", "final_summary"}
        assert res["report_id"] == "STUESO0520024332206LM" and res["final_summary"]["total_gaps"] >= 2
        g = next(x for x in res["gaps"] if x["clause_id"] == "10.1")
        assert g["verification_status"] == "CONFIRMED_REPORT_DEFECT"
        for fmt, head in (("pdf", b"%PDF"), ("xlsx", b"PK"), ("md", b"# Evaluation output")):
            d = client.get(body[f"download_{fmt}"])
            assert d.status_code == 200 and d.content.startswith(head), fmt
        assert json.loads(client.get(body["download_json"]).content) == res
        md = client.get(body["download_md"]).text.lower()
        assert "gpt" not in md and "model:" not in md and "openai" not in md
        again = client.post(f"{API}/engine-evaluations", files=_files())   # cache: no second AI call
        assert again.json()["from_cache"] is True and len(fake.calls) == 1
        assert client.get(f"{API}/engine-evaluations/{body['evaluation_id']}").json()["result"] == res
    finally:
        app.dependency_overrides.pop(get_ai_dep, None)


def test_engine_evaluation_error_message_is_neutral(client):
    async def boom(**kw):
        raise RuntimeError("insufficient_quota at OpenAI")
    app.dependency_overrides[get_ai_dep] = lambda: NS(client=NS(responses=NS(create=boom)))
    try:
        r = client.post(f"{API}/engine-evaluations", files=_files(), data={"use_cache": "false"})
        assert r.status_code == 503
        text = r.text.lower()
        assert "openai" not in text and "quota" not in text and "ai" not in r.json()["error"]["message"].lower().split()
    finally:
        app.dependency_overrides.pop(get_ai_dep, None)


def test_engine_evaluation_rejects_wrong_files(client):
    bad = {"effective_kb": ("a.json", b"{}", "application/json"),
           "report_evidence": ("b.json", b"not json", "application/json")}
    r = client.post(f"{API}/engine-evaluations", files=bad)
    assert r.status_code == 400