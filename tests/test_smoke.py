
import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield

def _login(c):
    r = c.post("/api/login", json={"username":"admin","password":"123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}

def test_pipeline():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    h = _login(c)
    assert len(c.get("/api/slices", headers=h).json()["items"]) >= 1
    # partial cannot release
    bad = c.post("/api/slices/1/release", headers=h, json={"consumer":"x"})
    assert bad.status_code == 409
    # register complete and release
    ok = c.post("/api/slices", headers=h, json={"slice_no":"SL-T-1","biz_date":"2026-11-04","partition_key":"mid","row_count":10,"expected_count":10})
    assert ok.status_code == 200
    sid = ok.json()["id"]
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer":"ads_x"})
    assert rel.status_code == 200
    assert any(w["source_slice"]=="SL-T-1" for w in c.get("/api/watermarks", headers=h).json()["items"])
