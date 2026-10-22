"""Dashboard Qualify tab: shows evidence, lets the owner approve or hold, never sends."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.state import StateManager
from tests.test_qualification_pilot import GOOD_SITE, assess_with, cfg, grooming


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


def seed(client):
    async def go():
        state = StateManager(db_path=str(client.db_path))
        await state.init_db()
        cid = await grooming(state)
        await assess_with(state, cfg(), cid, GOOD_SITE)
        return cid
    return asyncio.run(go())


def test_qualify_lists_evidence_and_waits_for_the_owner(client):
    cid = seed(client)
    data = client.get("/api/qualification").json()
    co = next(c for c in data["companies"] if c["id"] == cid)
    assert co["state"] == "needs_review"                      # Harvey proposes, the owner decides
    assert co["offer_fit"]["reason"] and co["facts"]
    assert all(f.get("url") and f.get("quote") is not None for f in co["facts"])
    assert data["counts"]["needs_review"] >= 1


def test_owner_can_approve_then_withdraw(client):
    cid = seed(client)
    r = client.post(f"/api/qualification/{cid}/approve", json={})
    assert r.status_code == 200 and r.json()["ok"]
    co = next(c for c in client.get("/api/qualification").json()["companies"] if c["id"] == cid)
    assert co["state"] == "qualified" and co["approved_fact_ids"]
    assert client.post(f"/api/qualification/{cid}/hold", json={"note": "skip"}).json()["ok"]
    co = next(c for c in client.get("/api/qualification").json()["companies"] if c["id"] == cid)
    assert co["state"] == "needs_review"


def test_approving_an_unassessed_company_is_refused(client):
    async def go():
        state = StateManager(db_path=str(client.db_path))
        await state.init_db()
        return await grooming(state)
    cid = asyncio.run(go())
    r = client.post(f"/api/qualification/{cid}/approve", json={})
    assert r.status_code == 400 and not r.json()["ok"]


def test_the_tab_is_wired_into_the_page(client):
    html = (Path(__file__).resolve().parent.parent / "harvey" / "web" / "index.html").read_text()
    js = (Path(__file__).resolve().parent.parent / "harvey" / "web" / "app.js").read_text()
    assert "showTab('qualify'" in html and 'id="qualify"' in html
    assert "case 'qualify': loadQualify()" in js
