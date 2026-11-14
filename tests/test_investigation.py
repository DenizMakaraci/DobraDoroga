import asyncio
import base64
import io
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from roadwatch import ai, store
from roadwatch.analysis import analyze, citation_valid
from roadwatch.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "case.sqlite3"))
    for name in [n for n in os.environ if n.startswith("ROADWATCH_") and n.endswith(("_MODEL", "_API_KEY", "_LABEL"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ROADWATCH_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ROADWATCH_MODEL", raising=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def case(client):
    return client.post("/api/demo").json()


def test_demo_overrun_is_exact_and_all_citations_resolve(client, case):
    response = client.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False})
    assert response.status_code == 200
    report = response.json()["reports"][-1]
    assert report["mode"] == "checks"
    assert report["totals"] == [{
        "scope": "roadway resurfacing", "basis": "before_tax",
        "authorized": "1100000.00", "paid": "1150000.00",
    }]
    assert len(report["findings"]) == 1
    assert "CAD 50,000.00" in report["findings"][0]["explanation"]
    assert all(citation_valid(c, case) for c in report["findings"][0]["citations"])
    exported = client.get(f"/api/projects/{case['id']}/export")
    assert exported.json()["reports"][-1]["source_catalog"] == case["evidence"]


def test_different_tax_basis_and_scope_never_mix(case):
    case["finances"][-1]["basis"] = "including_hst"
    result = analyze(case)
    assert result["findings"] == []
    assert result["totals"][0]["paid"] == "600000.00"
    case["finances"][-1]["basis"] = "before_tax"
    case["finances"][-1]["scope"] = "Watermain replacement"
    assert analyze(case)["findings"] == []


def test_multiple_awards_do_not_double_authorization(case):
    case["finances"].append({**case["finances"][0], "id": "another-award"})
    result = analyze(case)
    assert result["totals"] == []
    assert any("exactly one original award" in message for message in result["missing_records"])


def test_repeated_payment_is_review_lead_not_double_payment_conclusion(case):
    case["finances"].append({**case["finances"][-1], "id": "another-entry"})
    findings = analyze(case)["findings"]
    assert findings[0]["title"] == "Repeated payment reference"
    assert "entered twice" in findings[0]["alternative_explanation"]
    assert "Repeated references may inflate" in findings[1]["explanation"]


def test_financial_entry_requires_real_source_and_negative_payment_rejected(client, case):
    entry = {k: v for k, v in case["finances"][-1].items() if k != "id"}
    entry["quote"] = "This quotation is invented."
    assert client.post(f"/api/projects/{case['id']}/finances", json=entry).status_code == 422
    entry["quote"] = case["finances"][-1]["quote"]
    entry["amount"] = "-10"
    assert client.post(f"/api/projects/{case['id']}/finances", json=entry).status_code == 422
    entry["amount"] = "550000.00"
    assert client.post(f"/api/projects/{case['id']}/finances", json=entry).status_code == 201


def test_source_deletion_preserves_references_and_report_snapshots(client, case):
    base = f"/api/projects/{case['id']}"
    client.post(f"{base}/investigate", json={})
    doc_id = case["evidence"][0]["id"]
    assert client.delete(f"{base}/evidence/{doc_id}").status_code == 409
    assert client.delete(f"{base}/finances/{case['finances'][0]['id']}").status_code == 200
    updated = client.delete(f"{base}/evidence/{doc_id}").json()
    assert updated["revision"] > updated["reports"][-1]["revision"]
    assert any(d["id"] == doc_id for d in updated["reports"][-1]["source_catalog"])


def test_real_pdf_extraction_preserves_page_and_rejects_empty_scan(client, case):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Award: CAD 1000 before tax.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    payload = {
        "title": "Test award", "kind": "contract", "filename": "award.pdf",
        "file_base64": base64.b64encode(buffer.getvalue()).decode(),
    }
    response = client.post(f"/api/projects/{case['id']}/evidence", json=payload)
    assert response.status_code == 201
    doc = response.json()["evidence"][-1]
    assert "Award: CAD 1000 before tax." in doc["pages"][0]["text"]
    assert len(doc["sha256"]) == 64
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    payload["file_base64"] = base64.b64encode(buffer.getvalue()).decode()
    response = client.post(f"/api/projects/{case['id']}/evidence", json=payload)
    assert response.status_code == 422
    assert "OCR" in response.json()["detail"]


def test_api_validation_and_unconfigured_ai(client, case):
    base = f"/api/projects/{case['id']}"
    assert client.post("/api/projects", json={"name": " ", "road": "Road"}).status_code == 422
    assert client.post(f"{base}/investigate", json={"use_ai": True}).status_code == 422
    assert client.get("/api/projects/missing").status_code == 404
    assert client.post("/api/demo", headers={"origin": "https://unrelated.example"}).status_code == 403
    assert client.post(f"{base}/evidence", json={
        "title": "Bad source", "kind": "other", "text": "Source",
        "source_url": "javascript:alert(1)",
    }).status_code == 422
    assert client.post(f"{base}/observations", json={
        "observed_on": "2026-01-01", "latitude": 200, "longitude": -79,
        "severity": "severe", "description": "Large pothole at intersection.",
    }).status_code == 422


def test_ai_reads_evidence_and_rejects_fabricated_citations(case, monkeypatch):
    monkeypatch.setenv("ROADWATCH_API_KEY", "test-key")
    monkeypatch.setenv("ROADWATCH_MODEL", "test-model")
    evidence = case["evidence"][3]
    citation = {"evidence_id": evidence["id"], "page": 1, "quote": "One asphalt core measured 38 mm"}
    finding = {
        "title": "Sample needs follow-up",
        "explanation": "One reported sample is thinner than the specified thickness.",
        "alternative_explanation": "A repeat test could change the conclusion.",
        "next_step": "Request the repeat test and acceptance decision.",
        "citations": [citation],
    }
    result = {"findings": [finding, {**finding, "citations": [{**citation, "quote": "fabricated passage"}]}],
              "missing_records": ["Repeat test."]}
    call = SimpleNamespace(id="call-1", function=SimpleNamespace(
        name="read_page", arguments=json.dumps({"evidence_id": evidence["id"], "page": 1})
    ))
    tool_message = SimpleNamespace(tool_calls=[call])
    tool_message.model_dump = lambda **kw: {
        "role": "assistant", "tool_calls": [{"id": call.id, "type": "function",
                                          "function": vars(call.function)}],
    }
    messages = iter([tool_message, SimpleNamespace(tool_calls=None, content=json.dumps(result))])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=next(messages))])

    monkeypatch.setattr(ai, "AsyncOpenAI", FakeClient)
    report = asyncio.run(ai.investigate(case, analyze(case)))
    assert len(report["findings"]) == 1
    assert report["rejected_findings"] == 1
    assert report["activity"][0]["tool"] == "read_page"


def test_changed_evidence_cannot_receive_outdated_ai_report(client, case, monkeypatch):
    monkeypatch.setenv("ROADWATCH_API_KEY", "test")
    monkeypatch.setenv("ROADWATCH_MODEL", "test")

    async def changed_during_investigation(project, checks):
        store.append_item(project["id"], "observations", {"description": "New evidence"})
        return {"findings": [], "missing_records": [], "activity": [],
                "rejected_findings": 0, "model": "test"}

    monkeypatch.setattr(ai, "investigate", changed_during_investigation)
    response = client.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": True})
    assert response.status_code == 409
    assert store.get_project(case["id"])["reports"] == []


def test_import_allowlist_blocks_arbitrary_hosts(client, case):
    from roadwatch import documents
    assert documents.host_allowed("https://www.toronto.ca/legdocs/x.pdf")
    assert documents.host_allowed("https://secure.toronto.ca/council/report.pdf")
    assert not documents.host_allowed("https://toronto.ca.evil.example/x.pdf")
    assert not documents.host_allowed("https://example.com/x.pdf")
    response = client.post(f"/api/projects/{case['id']}/evidence/import",
                           json={"url": "https://example.com/report.pdf", "kind": "contract"})
    assert response.status_code == 422
    assert "official" in response.json()["detail"]
    response = client.post(f"/api/projects/{case['id']}/evidence/import",
                           json={"url": "http://www.toronto.ca/report.pdf", "kind": "contract"})
    assert response.status_code == 422


def test_import_parses_fetched_pdf_and_html(client, case, monkeypatch):
    from roadwatch import documents
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                             NameObject("/Subtype"): NameObject("/Type1"),
                                             NameObject("/BaseFont"): NameObject("/Helvetica")})})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Award of Doc123 to Example Ltd.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO(); writer.write(buffer)
    payloads = {"pdf": (buffer.getvalue(), "report.pdf", "application/pdf"),
                "html": (b"<html><head><style>x{}</style></head><body><h1>Award Summary</h1><p>Value: $1,000 &amp; more</p><script>bad()</script></body></html>", "page", "text/html")}

    async def fake_fetch(url):
        raw, name, ctype = payloads["pdf" if url.endswith(".pdf") else "html"]
        return raw, name, ctype, url
    monkeypatch.setattr(documents, "fetch_official", fake_fetch)
    base = f"/api/projects/{case['id']}/evidence/import"
    doc = client.post(base, json={"url": "https://www.toronto.ca/legdocs/report.pdf", "kind": "contract"}).json()["evidence"][-1]
    assert doc["title"] == "Award of Doc123 to Example Ltd."
    assert doc["source_url"] == "https://www.toronto.ca/legdocs/report.pdf"
    doc = client.post(base, json={"url": "https://www.torontoauditor.ca/report/x", "kind": "other"}).json()["evidence"][-1]
    assert "Award Summary" in doc["pages"][0]["text"] and "bad()" not in doc["pages"][0]["text"]
    assert "$1,000 & more" in doc["pages"][0]["text"]


def test_access_key_gates_api_and_sessions_work(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("ROADWATCH_ACCESS_KEY", "correct-horse")
    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 401
        session = client.get("/api/session").json()
        assert session["auth_required"] is True and session["signed_in"] is False and session["user"] is None
        assert session["accounts"] is False  # nobody has a password yet: the sign-in form opens on the access key
        assert client.get("/healthz").status_code == 200
        assert client.post("/api/login", json={"access_key": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"access_key": "correct-horse"}).status_code == 200
        assert client.get("/api/projects").status_code == 200
        assert client.get("/api/session").json()["signed_in"] is True
        client.post("/api/logout")
        assert client.get("/api/projects").status_code == 401
        for _ in range(8):
            client.post("/api/login", json={"access_key": "wrong"})
        assert client.post("/api/login", json={"access_key": "correct-horse"}).status_code == 429


def test_daily_ai_cap_and_project_delete(client, case, monkeypatch):
    monkeypatch.setenv("ROADWATCH_API_KEY", "k"); monkeypatch.setenv("ROADWATCH_MODEL", "m")
    monkeypatch.setenv("ROADWATCH_DAILY_AI_LIMIT", "1")

    async def fake(project, checks):
        return {"findings": [], "missing_records": [], "activity": [], "rejected_findings": 0, "model": "m"}
    monkeypatch.setattr(ai, "investigate", fake)
    base = f"/api/projects/{case['id']}"
    assert client.post(f"{base}/investigate", json={"use_ai": True}).status_code == 200
    second = client.post(f"{base}/investigate", json={"use_ai": True})
    assert second.status_code == 429 and "daily limit" in second.json()["detail"]
    assert client.post(f"{base}/investigate", json={"use_ai": False}).status_code == 200
    assert client.get("/api/config").json()["ai_runs_today"] == 1
    assert client.delete(base).status_code == 200
    assert client.get(base).status_code == 404
    assert client.delete(base).status_code == 404


def test_guest_mode_is_read_only_and_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "guest.sqlite3"))
    monkeypatch.setenv("ROADWATCH_ACCESS_KEY", "correct-horse")
    monkeypatch.delenv("ROADWATCH_GUEST", raising=False)
    from roadwatch import auth
    auth._attempts.clear()  # the lockout test above leaves this address throttled
    with TestClient(app) as admin, TestClient(app) as guest:
        assert admin.post("/api/login", json={"access_key": "correct-horse"}).status_code == 200
        case = admin.post("/api/demo").json()
        admin.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False})
        admin.post(f"/api/projects/{case['id']}/comments", json={"body": "Internal working note."})

        assert guest.get("/api/session").json()["guest_open"] is False
        assert guest.post("/api/guest").status_code == 403 and guest.get("/api/projects").status_code == 401

        monkeypatch.setenv("ROADWATCH_GUEST", "on")
        assert guest.get("/api/session").json()["guest_open"] is True
        entered = guest.post("/api/guest")
        assert entered.status_code == 200 and entered.json()["user"]["role"] == "guest"
        assert guest.get("/api/session").json()["user"]["role"] == "guest"

        # reading is open
        assert [p["id"] for p in guest.get("/api/projects").json()] == [case["id"]]
        seen = guest.get(f"/api/projects/{case['id']}").json()
        assert seen["evidence"] and seen["reports"][-1]["findings"]
        assert guest.get("/api/search", params={"q": "asphalt"}).json()["mode"] == "words"
        assert guest.get("/api/config").json()["semantic_search"] is False
        assert guest.get("/api/cities").status_code == 200 and guest.get("/api/me").json()["role"] == "guest"

        # everything else is closed
        id_ = case["id"]
        closed = [guest.post("/api/projects", json={"name": "x", "road": "y"}), guest.post("/api/demo"), guest.delete(f"/api/projects/{id_}"),
                  guest.post(f"/api/projects/{id_}/investigate", json={"use_ai": False}), guest.post(f"/api/projects/{id_}/open-data", json={}),
                  guest.post(f"/api/projects/{id_}/evidence", json={"title": "t", "kind": "other", "text": "x"}),
                  guest.post(f"/api/projects/{id_}/comments", json={"body": "hello"}), guest.get(f"/api/projects/{id_}/discussion"),
                  guest.get(f"/api/projects/{id_}/export"), guest.get("/api/members"), guest.get("/api/geocode", params={"q": "King Street"}),
                  guest.get("/api/search", params={"q": "asphalt", "mode": "meaning"}), guest.put("/api/me", json={"name": "Not a guest"})]
        assert [r.status_code for r in closed] == [403] * len(closed)
        assert "Guests can read" in closed[0].json()["detail"]
        assert "Internal working note." not in guest.get(f"/api/projects/{id_}").text

        # leaving, and switching guest mode off, both end the guest session
        again = TestClient(app)
        again.post("/api/guest")
        assert guest.post("/api/logout").status_code == 200 and guest.get("/api/projects").status_code == 401
        monkeypatch.setenv("ROADWATCH_GUEST", "off")
        assert again.get("/api/projects").status_code == 401 and admin.get("/api/projects").status_code == 200
