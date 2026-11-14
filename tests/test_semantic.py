"""Search by meaning. The embedding model is replaced by a word-hashing stand-in; on a plain MongoDB server the
nearest pieces come from the exact comparison, the same path Atlas uses while its vector index is building."""
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("pymongo")

from roadwatch import search, semantic, store, store_mongo  # noqa: E402
from roadwatch.server import app  # noqa: E402

needs_mongo = pytest.mark.skipif(not os.environ.get("ROADWATCH_TEST_MONGODB_URI"), reason="set ROADWATCH_TEST_MONGODB_URI")


def fake_embed(texts):
    vectors = []
    for text in texts:
        vector = [0.0] * 64
        for word in search.words(text.replace("\n", " "))[:8] + [w for w in search.fold(text).split() if len(w) > 2]:
            vector[int(hashlib.sha256(word.strip(".,:$").encode()).hexdigest(), 16) % 64] += 1.0
        vectors.append(vector)
    return vectors


@pytest.fixture
def meaning(monkeypatch):
    monkeypatch.setenv("ROADWATCH_EMBED_MODEL", "stand-in")
    monkeypatch.setenv("ROADWATCH_EMBED_DIMENSIONS", "64")
    monkeypatch.setenv("ROADWATCH_API_KEY", "not-used")
    monkeypatch.delenv("ROADWATCH_ACCESS_KEY", raising=False)
    monkeypatch.setattr(semantic, "embed", fake_embed)
    monkeypatch.setattr(semantic, "start", lambda: None)
    first = store.create_project({"name": "Rue Ontario", "road": "Rue Ontario", "contractor": ""})
    second = store.create_project({"name": "King Street", "road": "King Street", "contractor": ""})
    store.append_item(first["id"], "evidence", {"id": "evidence-a", "title": "Award report", "pages": [
        {"number": 1, "text": "Council summary of the tender and the schedule."},
        {"number": 2, "text": "Asphalt paving contract awarded to Pavages ABC for resurfacing the roadway."}]})
    store.append_item(second["id"], "evidence", {"id": "evidence-c", "title": "Lobbyist registry", "pages": [
        {"number": 1, "text": "Lobbyist communication with the councillor about a pharmacy licence."}, {"number": 2, "text": "   "}]})
    return first, second


def test_pieces_cut_pages_at_line_ends_and_keep_offsets():
    text = "line one\n" * 400 + "x" * 4000 + "\nlast\n"
    cut = semantic.pieces(text)
    assert max(len(piece) for _, piece in cut) <= semantic.PIECE_CHARACTERS and "".join(piece for _, piece in cut) == text
    assert all(text[offset:offset + len(piece)] == piece for offset, piece in cut) and semantic.pieces(" \n\n") == []


def test_search_by_meaning_is_off_without_mongodb_or_a_model(tmp_path, monkeypatch):
    monkeypatch.delenv("ROADWATCH_MONGODB_URI", raising=False)
    monkeypatch.setenv("ROADWATCH_EMBED_MODEL", "stand-in")
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "plain.sqlite3"))
    monkeypatch.delenv("ROADWATCH_ACCESS_KEY", raising=False)
    assert not semantic.available()
    with TestClient(app) as client:
        assert client.get("/api/config").json()["semantic_search"] is False
        assert client.get("/api/search", params={"q": "paving", "mode": "meaning"}).status_code == 422


@needs_mongo
def test_evidence_is_embedded_once_and_the_nearest_page_comes_first(meaning):
    first, second = meaning
    db = store_mongo._db()
    assert len(semantic.pending()) == 2 and semantic.index_pending() == 2
    assert db.chunks.count_documents({}) == 3 and semantic.pending() == {} and semantic.index_pending() == 0
    hits = semantic.search("asphalt resurfacing contract")
    assert (hits[0]["project_name"], hits[0]["evidence_id"], hits[0]["page"]) == ("Rue Ontario", "evidence-a", 2)
    assert 0 <= hits[-1]["score"] <= hits[0]["score"] <= 1 and len({(h["evidence_id"], h["page"]) for h in hits}) == len(hits) == 3
    assert semantic.search("pharmacy lobbyist")[0]["project_id"] == second["id"]
    assert len(semantic.search("asphalt", limit=1)) == 1


@needs_mongo
def test_text_already_embedded_is_not_sent_to_the_model_again(meaning, monkeypatch):
    first, second = meaning
    sent = []
    monkeypatch.setattr(semantic, "embed", lambda texts: sent.append(list(texts)) or fake_embed(texts))
    assert semantic.index_pending() == 2 and sum(map(len, sent)) == 3
    third = store.create_project({"name": "Queen Street", "road": "Queen Street", "contractor": ""})
    store.append_item(third["id"], "evidence", {"id": "evidence-z", "title": "Award report", "pages": [
        {"number": 1, "text": "Council summary of the tender and the schedule."},                                    # seen before, word for word
        {"number": 2, "text": "A new paragraph about streetcar track replacement."}]})
    sent.clear()
    assert semantic.index_pending() == 1 and sent == [["Award report\nA new paragraph about streetcar track replacement."]]
    assert store_mongo._db().chunks.count_documents({"project_id": third["id"]}) == 2
    assert semantic.search("streetcar track replacement")[0]["project_id"] == third["id"]


@needs_mongo
def test_chunks_follow_edits_and_deleted_cases(meaning):
    first, second = meaning
    db = store_mongo._db()
    semantic.index_pending()

    def rewrite(project):
        project["evidence"][0]["pages"][1]["text"] = "Sewer inspection video of the main under the roadway."
    store.mutate(first["id"], rewrite)
    assert db.chunks.count_documents({"project_id": first["id"]}) == 0 and len(semantic.pending()) == 1
    assert all(h["project_id"] == second["id"] for h in semantic.search("asphalt resurfacing contract"))
    assert semantic.index_pending() == 1 and semantic.search("sewer inspection video")[0]["page"] == 2
    store.delete_project(second["id"])
    assert db.chunks.count_documents({"project_id": second["id"]}) == 0
    assert {h["project_id"] for h in semantic.search("pharmacy lobbyist")} == {first["id"]}


@needs_mongo
def test_search_endpoint_by_meaning(meaning):
    with TestClient(app) as client:
        assert client.get("/api/config").json()["semantic_search"] is True
        early = client.get("/api/search", params={"q": "asphalt resurfacing", "mode": "meaning"}).json()
        assert early["hits"] == [] and early["indexing"] == 2
        semantic.index_pending()
        found = client.get("/api/search", params={"q": "asphalt resurfacing", "mode": "meaning"}).json()
        assert found["mode"] == "meaning" and found["indexing"] == 0 and found["hits"][0]["evidence_id"] == "evidence-a"
        assert client.get("/api/search", params={"q": "asphalt"}).json()["mode"] == "words"
