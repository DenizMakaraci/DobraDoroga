"""Search across the evidence of every case; runs on whichever store the suite is using."""
import os

import pytest
from fastapi.testclient import TestClient

from roadwatch import search, store
from roadwatch.server import app


@pytest.fixture
def cases(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "search.sqlite3"))
    monkeypatch.delenv("ROADWATCH_ACCESS_KEY", raising=False)
    first = store.create_project({"name": "Rue Ontario", "road": "Rue Ontario", "contractor": ""})
    second = store.create_project({"name": "King Street", "road": "King Street", "contractor": ""})
    store.append_item(first["id"], "evidence", {"id": "evidence-a", "title": "Award report", "pages": [
        {"number": 1, "text": "Summary of the tender.\nNothing about suppliers on this page."},
        {"number": 2, "text": "Contract awarded to Pavages Montréal inc. for $1,563,742.\nSecond bidder: Construction ABC ltée."}]})
    store.append_item(first["id"], "evidence", {"id": "evidence-b", "title": "Inspection", "pages": [
        {"number": 1, "text": "Paving completed; cracking observed in the montrealer lane."}]})
    store.append_item(second["id"], "evidence", {"id": "evidence-c", "title": "Pavages Montreal Inc. invoices", "pages": [
        {"number": 1, "text": "Invoice 14 from PAVAGES MONTREAL INC: $82,000."}, {"number": 2, "text": "Invoice 15: $91,000 for resurfacing."}]})
    return first, second


def test_words_are_folded_and_deduplicated():
    assert search.words("  Pavages MONTRÉAL, pavages inc. a ") == ["pavages", "montreal", "inc"]
    assert search.words("!!") == []


def test_search_finds_every_case_and_page_ignoring_case_and_accents(cases):
    first, second = cases
    hits = store.search_evidence("pavages montreal")
    assert {(h["project_name"], h["evidence_id"], h["page"]) for h in hits} == {
        ("Rue Ontario", "evidence-a", 2), ("King Street", "evidence-c", 1)}
    assert all("pavages" in h["snippet"].lower() for h in hits)
    assert {h["project_id"] for h in hits} == {first["id"], second["id"]}


def test_search_matches_whole_words_and_needs_every_word(cases):
    assert store.search_evidence("montreal cracking") == []
    assert [h["evidence_id"] for h in store.search_evidence("montrealer")] == ["evidence-b"]
    assert store.search_evidence("pavage") == []
    assert [(h["evidence_id"], h["page"]) for h in store.search_evidence("invoices resurfacing")] == [("evidence-c", 2)]
    assert store.search_evidence("") == [] and len(store.search_evidence("pavages", limit=1)) == 1


def test_search_follows_edits_and_deleted_cases(cases):
    first, second = cases

    def rewrite(project):
        project["evidence"][0]["pages"][1]["text"] = "Contract awarded to Asphalte Laval inc."
    store.mutate(first["id"], rewrite)
    assert [h["project_name"] for h in store.search_evidence("pavages montreal")] == ["King Street"]
    assert [h["page"] for h in store.search_evidence("asphalte laval")] == [2]
    store.delete_project(second["id"])
    assert store.search_evidence("pavages montreal") == []


def test_search_endpoint(cases):
    with TestClient(app) as client:
        found = client.get("/api/search", params={"q": " Montréal pavages "}).json()
        assert found["query"] == "Montréal pavages" and len(found["hits"]) == 2 and found["truncated"] is False
        assert client.get("/api/search", params={"q": "x"}).status_code == 422


def test_search_needs_sign_in(cases, monkeypatch):
    monkeypatch.setenv("ROADWATCH_ACCESS_KEY", "admin-key")
    with TestClient(app) as client:
        assert client.get("/api/search", params={"q": "pavages"}).status_code == 401
