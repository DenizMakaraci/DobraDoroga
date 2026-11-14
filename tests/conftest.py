import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def case_store(monkeypatch):
    """Tests never reach the MongoDB named in .env. With ROADWATCH_TEST_MONGODB_URI the whole suite runs on that
    server instead of SQLite, each test in its own database that is dropped afterwards."""
    uri = os.environ.get("ROADWATCH_TEST_MONGODB_URI")
    if not uri:
        monkeypatch.delenv("ROADWATCH_MONGODB_URI", raising=False)
        yield None
        return
    from pymongo import MongoClient
    name = f"roadwatch_test_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("ROADWATCH_MONGODB_URI", uri)
    monkeypatch.setenv("ROADWATCH_MONGODB_DB", name)
    yield name
    MongoClient(uri).drop_database(name)
