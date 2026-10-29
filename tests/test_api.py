import uuid

from app.middleware.rate_limit import SlidingWindowLimiter
from app.utils.pdf_utils import detect_standards
from app.utils.text_utils import normalize_prompt
from tests.conftest import make_pdf

API = "/api/v1"


def _pdf(name, text):
    return (name, make_pdf(text + f"\nuid {uuid.uuid4()}"), "application/pdf")


def _upload_standard(client, text, **form):
    return client.post(f"{API}/standards", files={"file": _pdf("std.pdf", text)}, data=form)


def test_upload_standard_autodetects_name_and_dedupes(client):
    pdf = make_pdf("IEC 60335-1:2020\nHousehold appliances - Safety - Part 1")
    r1 = client.post(f"{API}/standards", files={"file": ("a.pdf", pdf, "application/pdf")})
    assert r1.status_code == 201, r1.text
    body = r1.json()
    assert body["standard"]["name"] == "IEC 60335-1"
    assert body["standard"]["edition"] == "2020"
    assert body["duplicate"] is False

    r2 = client.post(f"{API}/standards", files={"file": ("b.pdf", pdf, "application/pdf")})
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["standard"]["id"] == body["standard"]["id"]


def test_rejects_non_pdf(client):
    r = client.post(f"{API}/standards", files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_file"


def test_full_chat_flow_with_cache_and_memory(client, fake_ai):
    std = _upload_standard(client, "IEC 60335-2-7:2019 washing machines").json()["standard"]
    user = f"u-{uuid.uuid4()}"

    # 1) report upload, no chat_id -> new chat, standard auto-detected from the report
    r = client.post(f"{API}/chat",
                    data={"user_id": user, "query": "Evaluate this report"},
                    files={"report_file": _pdf("report.pdf", "Test Report\nStandard: IEC 60335-2-7:2019")})
    assert r.status_code == 200, r.text
    first = r.json()
    chat_id = first["chat_id"]
    assert first["new_chat"] is True
    assert first["from_cache"] is False
    assert first["standards_auto_detected"] is True
    assert [s["id"] for s in first["standards_used"]] == [std["id"]]
    assert len(fake_ai.asks) == 1
    assert len(fake_ai.asks[0]["files"]) == 2          # standard + report sent once

    # 2) same prompt (different spacing/case) in same chat -> cache, AI not called
    r = client.post(f"{API}/chat", data={"user_id": user, "chat_id": chat_id,
                                         "query": "  evaluate THIS report? "})
    assert r.json()["from_cache"] is True
    assert r.json()["answer"] == first["answer"]
    assert len(fake_ai.asks) == 1
    assert len(fake_ai.appended) == 1                   # thread kept in sync

    # 3) new follow-up -> AI called on the SAME conversation, files NOT re-sent
    r = client.post(f"{API}/chat", data={"user_id": user, "chat_id": chat_id,
                                         "query": "Why did clause 19 fail?"})
    assert r.json()["from_cache"] is False
    assert len(fake_ai.asks) == 2
    assert fake_ai.asks[1]["conv"] == fake_ai.asks[0]["conv"]
    assert fake_ai.asks[1]["files"] == []

    # 4) use_cache=false forces AI
    r = client.post(f"{API}/chat", data={"user_id": user, "chat_id": chat_id,
                                         "query": "Evaluate this report", "use_cache": "false"})
    assert r.json()["from_cache"] is False
    assert len(fake_ai.asks) == 3

    # 5) history: every turn stored
    msgs = client.get(f"{API}/chats/{chat_id}/messages", params={"user_id": user}).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 4
    assert msgs[3]["from_cache"] is True

    chats = client.get(f"{API}/chats", params={"user_id": user}).json()
    assert len(chats) == 1 and chats[0]["id"] == chat_id

    reports = client.get(f"{API}/reports", params={"user_id": user}).json()
    assert len(reports) == 3
    assert reports[-1]["cache_hits"] == 1


def test_cache_shared_across_chats_for_same_report(client, fake_ai):
    _upload_standard(client, "IEC 60335-2-24:2020 refrigerators")
    report = _pdf("fridge.pdf", "Report for IEC 60335-2-24")
    a = client.post(f"{API}/chat", data={"user_id": "u1", "query": "Summary"},
                    files={"report_file": report}).json()
    b = client.post(f"{API}/chat", data={"user_id": "u2", "query": "summary"},
                    files={"report_file": report}).json()
    assert a["chat_id"] != b["chat_id"]
    assert b["from_cache"] is True
    assert len(fake_ai.asks) == 1


def test_explicit_standard_files_and_ids(client, fake_ai):
    s1 = _upload_standard(client, "EN 60335-1:2012").json()["standard"]
    r = client.post(
        f"{API}/chat",
        data={"user_id": "u3", "query": "Compare", "standard_ids": s1["id"]},
        files=[("standard_files", _pdf("ul.pdf", "UL 2157 standard")),
               ("report_file", _pdf("r.pdf", "some report"))],
    )
    assert r.status_code == 200, r.text
    names = {s["name"] for s in r.json()["standards_used"]}
    assert names == {"EN 60335-1", "UL 2157"}
    assert r.json()["standards_auto_detected"] is False


def test_more_than_two_standards_rejected(client):
    files = [("standard_files", _pdf(f"s{i}.pdf", f"IEC 6100{i}")) for i in range(3)]
    r = client.post(f"{API}/chat", data={"user_id": "u", "query": "x"}, files=files)
    assert r.status_code == 400
    assert "At most 2" in r.json()["error"]["message"]


def test_other_user_cannot_use_chat(client):
    r = client.post(f"{API}/chat", data={"user_id": "owner", "query": "hello"})
    chat_id = r.json()["chat_id"]
    r = client.post(f"{API}/chat", data={"user_id": "intruder", "chat_id": chat_id, "query": "hi"})
    assert r.status_code == 403
    r = client.post(f"{API}/chat", data={"user_id": "owner", "chat_id": "nope", "query": "hi"})
    assert r.status_code == 404


def test_missing_standard_warning(client):
    r = client.post(f"{API}/chat", data={"user_id": "u9", "query": "check"},
                    files={"report_file": _pdf("r.pdf", "Tested to IEC 60335-2-99:2021")})
    body = r.json()
    assert body["standards_used"] == []
    assert any("IEC 60335-2-99" in w for w in body["warnings"])


def test_rate_limiter():
    lim = SlidingWindowLimiter(max_requests=2, window_seconds=10)
    assert lim.hit("k", now=0)[0] and lim.hit("k", now=1)[0]
    allowed, _, retry = lim.hit("k", now=2)
    assert not allowed and retry == 8
    assert lim.hit("k", now=10.5)[0]


def test_helpers():
    names = [d.name for d in detect_standards("as per IEC 60335-2-7:2019 and EN 60335-1 ; is 10 items")]
    assert names == ["IEC 60335-2-7", "EN 60335-1"]
    assert normalize_prompt("  Evaluate   This!! ") == "evaluate this"


# ----------------------------------------------------------- parent / child
from app.utils.pdf_utils import parse_standard_name  # noqa: E402
from tests.conftest import STORAGE_DIR  # noqa: E402

PART2_TEXT = ("IEC 60335-2-7:2012\nParticular requirements for washing machines\n"
              "This part is to be used in conjunction with IEC 60335-1:2010")


def test_parse_standard_name():
    p = parse_standard_name("IEC 60335-2-7")
    assert (p.family, p.part_type, p.part_number) == ("IEC 60335", "part2", "2-7")
    assert parse_standard_name("IEC 60335-1").part_type == "part1"
    assert parse_standard_name("IEC 61000-3-2").part_type == "part"
    assert parse_standard_name("UL 2157").part_type == "standalone"


def test_part2_first_then_part1_links_by_edition(client):
    r = _upload_standard(client, PART2_TEXT).json()
    child = r["standard"]
    assert child["part_type"] == "part2"
    assert child["parent_edition_hint"] == "2010"
    assert child["storage_key"] == "standards/IEC_60335/part2/IEC_60335-2-7_2012.pdf"
    assert (STORAGE_DIR / child["storage_key"]).exists()
    assert r["parent"] is None and "not uploaded yet" in r["warnings"][0]

    # wrong edition of Part 1 -> linked as fallback
    p2020 = _upload_standard(client, "IEC 60335-1:2020 General requirements").json()
    assert p2020["standard"]["storage_key"] == "standards/IEC_60335/part1/IEC_60335-1_2020.pdf"
    assert [c["id"] for c in p2020["children"]] == [child["id"]]

    # correct edition arrives -> child re-linked to 2010
    p2010 = _upload_standard(client, "IEC 60335-1:2010 General requirements").json()
    got = client.get(f"{API}/standards/{child['id']}").json()
    assert got["parent"]["id"] == p2010["standard"]["id"]
    assert got["parent"]["edition"] == "2010"

    tree = client.get(f"{API}/standards/tree").json()
    assert tree[0]["family"] == "IEC 60335"
    assert len(tree[0]["part1"]) == 2 and len(tree[0]["part2"]) == 1


def test_report_brings_part2_with_parent_and_saves_output(client, fake_ai):
    p1 = _upload_standard(client, "IEC 60335-1:2010").json()["standard"]
    p2 = _upload_standard(client, PART2_TEXT).json()["standard"]
    r = client.post(f"{API}/chat", data={"user_id": "amit", "query": "Evaluate"},
                    files={"report_file": _pdf("washer report.pdf",
                                               "TRF\nStandard: IEC 60335-2-7:2012")}).json()
    assert [s["id"] for s in r["standards_used"]] == [p1["id"], p2["id"]]   # parent first
    assert r["standards_auto_detected"] is True
    assert len(fake_ai.asks[0]["files"]) == 3
    assert "Part 2 clauses replace/modify" in fake_ai.asks[0]["text"]

    # report kept separately with its output
    report_dir = STORAGE_DIR / "reports" / r["report"]["id"]
    assert (report_dir / "washer_report.pdf").exists()
    out = STORAGE_DIR / r["output_path"]
    assert out.parent == report_dir / "outputs"
    assert "answer #1" in out.read_text() and "IEC 60335-2-7:2012" in out.read_text()
    assert not list(STORAGE_DIR.rglob("*.json"))


def test_explicit_part2_id_brings_parent(client):
    p1 = _upload_standard(client, "IEC 60335-1:2010").json()["standard"]
    p2 = _upload_standard(client, PART2_TEXT).json()["standard"]
    r = client.post(f"{API}/chat", data={"user_id": "u", "query": "q", "standard_ids": p2["id"]}).json()
    assert [s["id"] for s in r["standards_used"]] == [p1["id"], p2["id"]]
    assert r["standards_used"][1]["parent_id"] == p1["id"]
