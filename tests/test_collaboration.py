"""Team workspace: member keys and roles, discussion anchored to evidence, finding statuses, AI context."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from roadwatch import ai
from roadwatch.server import app


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "team.sqlite3"))
    monkeypatch.setenv("ROADWATCH_ACCESS_KEY", "admin-key")
    for name in [n for n in os.environ if n.startswith("ROADWATCH_") and n.endswith(("_MODEL", "_API_KEY", "_LABEL"))]:
        monkeypatch.delenv(name, raising=False)
    with TestClient(app) as client:
        assert client.post("/api/login", json={"access_key": "admin-key"}).json()["user"]["role"] == "admin"
        yield client


def invite_code(payload):
    return payload["invite_link"].split("#invite=")[1]


def enrol(admin, name, role, password="long enough passphrase"):
    """Admin invites; the person opens the link and sets a password; returns (member, signed-in client)."""
    email = f"{name.split()[0].lower()}@example.org"
    created = admin.post("/api/members", json={"name": name, "email": email, "role": role}).json()
    client = TestClient(app)
    assert client.post("/api/invite/accept", json={"code": invite_code(created), "password": password}).status_code == 200
    return {**created, "email": email, "password": password}, client


def signed_in_as(email, password):
    client = TestClient(app)
    client.post("/api/login", json={"email": email, "password": password})
    return client


def test_accounts_invites_passwords_and_roles(admin):
    created = admin.post("/api/members", json={"name": "Jane Counsel", "email": "Jane@Example.org", "role": "counsel"}).json()
    assert "#invite=" in created["invite_link"] and created["emailed"] is False and created["email"] == "jane@example.org"
    listed = admin.get("/api/members").json()["members"]
    assert listed[0]["invited"] == 1 and listed[0]["has_password"] == 0 and not {"key", "key_hash", "password_hash", "invite_hash"} & set(listed[0])
    assert admin.post("/api/members", json={"name": "Dup", "email": "jane@example.org"}).status_code == 409

    code = invite_code(created)
    anon = TestClient(app)
    assert anon.get(f"/api/invite/{code}").json() == {"name": "Jane Counsel", "email": "jane@example.org", "role": "counsel", "reset": False}
    assert anon.post("/api/invite/accept", json={"code": code, "password": "short"}).status_code == 422
    assert anon.post("/api/invite/accept", json={"code": "not-a-real-code", "password": "long enough passphrase"}).status_code == 410
    accepted = anon.post("/api/invite/accept", json={"code": code, "password": "long enough passphrase"})
    assert accepted.status_code == 200 and accepted.json()["user"]["role"] == "counsel"
    assert anon.get("/api/session").json()["user"]["email"] == "jane@example.org"  # signed in by accepting
    assert anon.get(f"/api/invite/{code}").status_code == 410  # single use

    assert TestClient(app).post("/api/login", json={"email": "jane@example.org", "password": "wrong password"}).status_code == 401
    jane = signed_in_as("JANE@example.org", "long enough passphrase")
    assert jane.get("/api/session").json()["user"]["name"] == "Jane Counsel"
    assert jane.post("/api/members", json={"name": "x", "email": "x@example.org"}).status_code == 403  # not an admin

    # profile and password change; the change signs out every other session
    assert jane.put("/api/me", json={"name": "Jane C."}).json()["name"] == "Jane C."
    other_session = signed_in_as("jane@example.org", "long enough passphrase")
    assert jane.put("/api/me/password", json={"current_password": "nope", "new_password": "another long passphrase"}).status_code == 401
    assert jane.put("/api/me/password", json={"current_password": "long enough passphrase", "new_password": "another long passphrase"}).status_code == 200
    assert other_session.get("/api/projects").status_code == 401 and jane.get("/api/projects").status_code == 200

    viewer, vic = enrol(admin, "Vic Viewer", "viewer")
    assert vic.get("/api/projects").status_code == 200
    denied = vic.post("/api/projects", json={"name": "No", "road": "No"})
    assert denied.status_code == 403 and "Viewers" in denied.json()["detail"]

    # admin-issued password reset link: old password dead, sessions ended, link sets a new one
    reset = admin.put(f"/api/members/{viewer['id']}", json={"reset_password": True}).json()
    assert vic.get("/api/projects").status_code == 401
    assert TestClient(app).get(f"/api/invite/{invite_code(reset)}").json()["reset"] is True
    assert TestClient(app).post("/api/invite/accept", json={"code": invite_code(reset), "password": "brand new passphrase"}).status_code == 200
    assert signed_in_as(viewer["email"], "brand new passphrase").get("/api/projects").status_code == 200

    admin.put(f"/api/members/{viewer['id']}", json={"active": False})
    assert signed_in_as(viewer["email"], "brand new passphrase").get("/api/projects").status_code == 401  # deactivated
    assert admin.get("/api/members").json()["members"][1]["active"] == 0

    # forgot-password without SMTP never leaks whether the address exists
    for email in ("jane@example.org", "nobody@example.org"):
        answer = TestClient(app).post("/api/password/forgot", json={"email": email}).json()
        assert answer["ok"] and "administrator" in answer["message"]
    # the bootstrap admin key still works and cannot have a profile
    assert admin.put("/api/me", json={"name": "Root"}).status_code == 422


def test_discussion_is_anchored_cited_and_role_labelled(admin):
    case = admin.post("/api/demo").json()
    report = admin.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    finding = report["findings"][0]
    assert finding["key"] and len(finding["key"]) == 10
    evidence = case["evidence"][3]

    member, ina = enrol(admin, "Ina Investigator", "investigator")
    bad = ina.post(f"/api/projects/{case['id']}/comments", json={"body": "See page", "citations": [{"evidence_id": evidence["id"], "page": 1, "quote": "not on the page"}]})
    assert bad.status_code == 422
    good = ina.post(f"/api/projects/{case['id']}/comments", json={
        "anchor_type": "finding", "anchor_id": finding["key"], "anchor_label": finding["title"],
        "body": "Core C-4 measured 38 mm; need the repeat test before drawing anything.",
        "citations": [{"evidence_id": evidence["id"], "page": 1, "quote": "One asphalt core measured 38 mm"}]}).json()
    assert good["author_name"] == "Ina Investigator" and good["author_role"] == "investigator" and good["opinion"] is False

    opinion = ina.post(f"/api/projects/{case['id']}/comments", json={"body": "This is obviously fraud by the contractor."}).json()
    assert opinion["opinion"] is True  # conclusory words, no citation -> labelled as opinion, not blocked

    reply = admin.post(f"/api/projects/{case['id']}/comments", json={"parent_id": good["id"], "anchor_type": "finding", "anchor_id": finding["key"], "body": "Agreed; FOI filed."}).json()
    assert reply["parent_id"] == good["id"]

    assessment = ina.post(f"/api/projects/{case['id']}/comments", json={"kind": "assessment", "anchor_type": "case", "body": "",
        "assessment": {"summary": "Records show payments exceeding authorization by CAD 50,000; evaluation report missing.",
                       "legal_basis": "Municipal Code ch. 195", "risk": "medium", "recommended_action": "Request the amendment register.", "status": "draft"}}).json()
    assert assessment["kind"] == "assessment" and assessment["assessment"]["risk"] == "medium"

    thread = admin.get(f"/api/projects/{case['id']}/discussion").json()
    assert len(thread["comments"]) == 4 and thread["me"]["role"] == "admin" and "cite" in thread["notice"]

    # authors edit their own posts; others cannot; admins can delete anything
    assert admin.put(f"/api/projects/{case['id']}/comments/{good['id']}", json={"body": "x"}).status_code == 200  # admin
    other, other_client = enrol(admin, "Other Person", "investigator")
    assert other_client.put(f"/api/projects/{case['id']}/comments/{good['id']}", json={"body": "x"}).status_code == 403
    assert admin.delete(f"/api/projects/{case['id']}/comments/{opinion['id']}").json()["deleted"] == opinion["id"]

    # finding status board
    status = ina.put(f"/api/projects/{case['id']}/findings/{finding['key']}/status", json={"status": "investigating", "assignee_id": member["id"], "note": "Checking the ledger", "title": finding["title"]}).json()
    assert status["updated_by"] == "Ina Investigator"
    board = admin.get(f"/api/projects/{case['id']}/discussion").json()["statuses"]
    assert board[0]["status"] == "investigating" and board[0]["assignee_name"] == "Ina Investigator"
    assert ina.put(f"/api/projects/{case['id']}/findings/{finding['key']}/status", json={"status": "open", "assignee_id": "nobody"}).status_code == 404

    # viewers can discuss but not change the case
    viewer, vic = enrol(admin, "Vic Viewer", "viewer")
    assert vic.post(f"/api/projects/{case['id']}/comments", json={"body": "Question: was the repeat test ever done?"}).status_code == 201
    assert vic.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False}).status_code == 403

    # the export carries the discussion; the AI context gets it as leads
    exported = admin.get(f"/api/projects/{case['id']}/export").json()
    assert len(exported["discussion"]) == 4 and exported["finding_statuses"][0]["status"] == "investigating"
    notes = ai.team_notes(exported)
    assert notes["finding_statuses"][0]["status"] == "investigating"
    assert any(n.get("assessment") for n in notes["comments"]) and all("by" in n for n in notes["comments"])
    # statuses survive a re-run: the key is stable
    rerun = admin.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    assert rerun["findings"][0]["key"] == finding["key"]


def test_local_mode_has_a_single_admin_user(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "local.sqlite3"))
    monkeypatch.delenv("ROADWATCH_ACCESS_KEY", raising=False)
    with TestClient(app) as client:
        session = client.get("/api/session").json()
        assert session["user"]["role"] == "admin" and session["auth_required"] is False
        case = client.post("/api/demo").json()
        posted = client.post(f"/api/projects/{case['id']}/comments", json={"body": "Local note"}).json()
        assert posted["author_name"] == "Local user"
        assert client.post("/api/members", json={"name": "x", "email": "x@example.org"}).status_code == 422  # accounts need sign-in enabled


def test_open_signup_creates_viewer_accounts_with_rules_and_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "signup.sqlite3"))
    monkeypatch.setenv("ROADWATCH_ACCESS_KEY", "admin-key")
    monkeypatch.delenv("ROADWATCH_SMTP_HOST", raising=False)
    monkeypatch.setenv("ROADWATCH_SIGNUP", "invite")
    with TestClient(app) as client:
        assert client.get("/api/session").json()["signup_open"] is False
        closed = client.post("/api/signup", json={"name": "Sam", "email": "sam@example.org", "password": "a long passphrase", "accept_rules": True})
        assert closed.status_code == 403 and "invite-only" in closed.json()["detail"]
    monkeypatch.setenv("ROADWATCH_SIGNUP", "open")
    from roadwatch import auth
    monkeypatch.setattr(auth, "_signups", __import__("collections").defaultdict(list))
    with TestClient(app) as client:
        session = client.get("/api/session").json()
        assert session["signup_open"] is True and session["signup_role"] == "viewer" and "cite" in session["rules"]
        assert client.post("/api/signup", json={"name": "Sam", "email": "sam@example.org", "password": "a long passphrase"}).status_code == 422  # rules not accepted
        assert client.post("/api/signup", json={"name": "Sam", "email": "sam@example.org", "password": "short", "accept_rules": True}).status_code == 422
        created = client.post("/api/signup", json={"name": "Sam Citizen", "email": "Sam@Example.org", "password": "a long passphrase", "accept_rules": True})
        assert created.status_code == 201 and created.json()["user"]["role"] == "viewer"  # no SMTP -> active and signed in at once
        assert client.get("/api/session").json()["user"]["email"] == "sam@example.org"
        assert client.post("/api/signup", json={"name": "Dup", "email": "sam@example.org", "password": "a long passphrase", "accept_rules": True}).status_code == 409
        # a self-registered viewer can read and discuss but not change cases; admin can promote
        assert client.post("/api/projects", json={"name": "x", "road": "y"}).status_code == 403
        admin = TestClient(app)
        admin.post("/api/login", json={"access_key": "admin-key"})
        case = admin.post("/api/demo").json()
        assert client.post(f"/api/projects/{case['id']}/comments", json={"body": "I live on this street; the surface failed within a year."}).status_code == 201
        sam = admin.get("/api/members").json()["members"][0]
        admin.put(f"/api/members/{sam['id']}", json={"role": "investigator"})
        assert client.post("/api/projects", json={"name": "Mine", "road": "Cedar Avenue"}).status_code == 201
        # per-IP throttle: 5 accounts an hour
        for i in range(4):
            assert client.post("/api/signup", json={"name": f"P{i}", "email": f"p{i}@example.org", "password": "a long passphrase", "accept_rules": True}).status_code == 201
        assert client.post("/api/signup", json={"name": "P9", "email": "p9@example.org", "password": "a long passphrase", "accept_rules": True}).status_code == 429


def test_signup_with_email_requires_verification(tmp_path, monkeypatch):
    from roadwatch import auth
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "verify.sqlite3"))
    monkeypatch.setenv("ROADWATCH_ACCESS_KEY", "admin-key")
    monkeypatch.setenv("ROADWATCH_SIGNUP", "open")
    monkeypatch.setenv("ROADWATCH_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("ROADWATCH_MAIL_FROM", "roadwatch@example.org")
    sent = []
    monkeypatch.setattr(auth, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr(auth, "_signups", __import__("collections").defaultdict(list))  # throttle state from earlier tests
    with TestClient(app) as client:
        created = client.post("/api/signup", json={"name": "Vera", "email": "vera@example.org", "password": "a long passphrase", "accept_rules": True}).json()
        assert created["pending_verification"] is True and sent[0][0] == "vera@example.org" and "#verify=" in sent[0][2]
        assert client.get("/api/session").json()["signed_in"] is False
        blocked = TestClient(app).post("/api/login", json={"email": "vera@example.org", "password": "a long passphrase"})
        assert blocked.status_code == 403 and "confirm" in blocked.json()["detail"].lower()
        code = sent[0][2].split("#verify=")[1].split()[0]
        verified = TestClient(app)
        assert verified.get(f"/api/verify/{code}").json()["user"]["name"] == "Vera"
        assert verified.get("/api/session").json()["signed_in"] is True
        assert TestClient(app).post("/api/login", json={"email": "vera@example.org", "password": "a long passphrase"}).status_code == 200
        assert TestClient(app).get(f"/api/verify/{code}").status_code == 410  # single use


def test_observations_have_their_own_threads(admin):
    case = admin.post("/api/demo").json()
    observation = admin.post(f"/api/projects/{case['id']}/observations", json={
        "observed_on": "2026-05-01", "latitude": 43.67, "longitude": -79.40, "severity": "severe",
        "description": "Alligator cracking across both lanes at the west limit."}).json()["observations"][-1]
    posted = admin.post(f"/api/projects/{case['id']}/comments", json={
        "anchor_type": "observation", "anchor_id": observation["id"], "anchor_label": f"Observation {observation['observed_on']}",
        "body": "Matches the 311 cluster at Bedford; worth a photo on the next visit."})
    assert posted.status_code == 201 and posted.json()["anchor_type"] == "observation"
    assert admin.post(f"/api/projects/{case['id']}/comments", json={"anchor_type": "photo", "anchor_id": "x", "body": "no"}).status_code == 422
    thread = admin.get(f"/api/projects/{case['id']}/discussion").json()["comments"]
    assert [c["anchor_id"] for c in thread] == [observation["id"]]


def test_dossiers_and_deep_investigation_pipeline(admin, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from roadwatch import dossier, store
    from roadwatch.analysis import analyze
    case = admin.post("/api/demo").json()
    admin.post(f"/api/projects/{case['id']}/comments", json={"body": "Was the repeat core test ever done?"})
    case = store.get_project(case["id"])
    d = dossier.build(case)
    assert d["documents_full_text"] and d["money"][0]["paid_minus_authorised"] == "50000.00"
    assert any(e["kind"] == "completion" for e in d["timeline"])

    evidence = case["evidence"][3]
    cite = {"evidence_id": evidence["id"], "page": 1, "quote": "One asphalt core measured 38 mm"}
    finding = {"title": "Thin core without a recorded repeat test", "explanation": "One core at 38 mm vs 50 mm specified; no repeat result on file.",
               "alternative_explanation": "The repeat test may have passed and simply not been filed.", "next_step": "Obtain the repeat test.",
               "citations": [cite], "confidence": "plausible", "tests_performed": ["checked inspection note", "checked payment ledger"],
               "what_would_change": "The repeat test result and acceptance decision."}
    scripted = [
        json.dumps({"hypotheses": [{"title": "Core thickness below spec was accepted without retest", "rationale": "r", "confirm_if": "no retest", "refute_if": "retest passed", "queries": ["38 mm"]}],
                    "team_questions": ["Was the repeat core test ever done?"]}),
        "TOOL:search_evidence",  # hypothesis loop: one tool call then the verdict
        json.dumps({"verdict": "plausible", "evidence_for": [{**cite, "note": "inspection note"}], "evidence_against": [], "tests_performed": ["read inspection note"], "notes": ""}),
        json.dumps({"findings": [finding, {**finding, "title": "Fabricated", "citations": [{**cite, "quote": "not there"}]}],
                    "answers": [{"question": "Was the repeat core test ever done?", "answer": "No result is on file; the inspector requested one.", "citations": [cite]}],
                    "missing_records": ["Repeat core test result"]}),
        json.dumps({"findings": [{**finding, "confidence": "speculative"}], "answers": [{"question": "Was the repeat core test ever done?", "answer": "Not on file.", "citations": []}],
                    "missing_records": ["Repeat core test result"], "review_notes": ["Lowered confidence: acceptance decision not read."]}),
    ]
    responses = iter(scripted)

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def close(self):
            pass

        async def create(self, **kwargs):
            text = next(responses)
            if text.startswith("TOOL:"):
                call = SimpleNamespace(id="c1", function=SimpleNamespace(name=text[5:], arguments=json.dumps({"query": "38 mm"})))
                message = SimpleNamespace(tool_calls=[call], content="")
                message.model_dump = lambda **kw: {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": vars(call.function)}]}
                return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content=text), finish_reason="stop")])

    monkeypatch.setenv("ROADWATCH_API_KEY", "k"); monkeypatch.setenv("ROADWATCH_MODEL", "test-model")
    from roadwatch import ai
    monkeypatch.setattr(ai, "AsyncOpenAI", FakeClient)
    result = asyncio.run(ai.investigate(case, analyze(case), depth="deep"))
    assert result["depth"] == "deep" and [p["pass"] for p in result["passes"]] == ["plan", "test", "synthesis", "review"]
    assert result["passes"][1]["results"][0]["verdict"] == "plausible" and result["passes"][1]["results"][0]["tool_calls"] == 1
    assert len(result["findings"]) == 1 and result["findings"][0]["confidence"] == "speculative"  # reviewer's call stands
    assert result["findings"][0]["tests_performed"] and result["answers"][0]["question"].startswith("Was the repeat")
    assert result["activity"][0]["tool"] == "search_evidence" and "Lowered confidence" in result["passes"][3]["notes"][0]


def test_ml_anomalies_and_team_calibration(admin, monkeypatch):
    from roadwatch import ml
    rows = [{"road_work": True, "bidders": b, "spread_pct": s, "supplier": w, "award_amount": 1, "bidder_names": "; ".join(n), "ocid": f"o{i}", "date": "2025", "title": "t", "winner_is_lowest": True}
            for i, (b, s, w, n) in enumerate([(4, 8.0, "A", "A; B; C; D")] * 10 + [(1, None, "X", "X"), (3, 0.4, "A", "A; B; C")])]
    project = {"open_data": [{"key": "bids", "records": rows}]}
    scores = ml.anomaly_scores(project)
    assert scores["baseline"]["median_bidders"] == 4
    flags = {t["ocid"]: t["flags"] for t in scores["unusual_tenders"]}
    assert "far fewer bidders than typical" in flags["o10"] and "unusually tight low bids" in flags["o11"]
    assert scores["unusual_suppliers"] and scores["unusual_suppliers"][0]["supplier"] == "A"
    # calibration abstains without enough verdicts, then learns from them
    finding = {"title": "Road tenders awarded with a single bidder", "origin": "open_data", "confidence": "plausible"}
    assert ml.calibration([dict(finding)])["available"] is False
    data = [(ml.features({"title": "Road tenders awarded with a single bidder"}), 1.0)] * 8 + [(ml.features({"title": "Road-damage complaints roughly doubled"}), 0.0)] * 8
    model = ml.train(data)
    assert ml.predict(model, {"title": "Road tenders awarded with a single bidder"}) > 0.8 > ml.predict(model, {"title": "Road-damage complaints roughly doubled"})
    monkeypatch.setattr(ml, "training_set", lambda: data)
    result = ml.calibration([dict(finding)])
    assert result["available"] and result["labels"] == 16 and result["influential_patterns"][0]["pattern"] in ("single_bidder", "complaint_spike")
