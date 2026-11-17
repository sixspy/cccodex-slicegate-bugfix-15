"""日界锁定口径测试：甲方案钉死、跨日残留清理、互斥争用、偏差登记、五处读数一致。"""
import threading

import pytest
from fastapi.testclient import TestClient

import day_boundary
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr("app.DB_PATH", db)
    monkeypatch.setattr("day_boundary.DB_PATH", db)
    monkeypatch.setattr(day_boundary, "BOUNDARY_LOCK_TIMEOUT", 0.05)
    init_db()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def _h(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _complete_slice(c, h, no="SL-T-1", biz_date="2026-11-02"):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": no, "biz_date": biz_date, "partition_key": "mid",
        "row_count": 10, "expected_count": 10})
    assert r.status_code == 200
    return r.json()["id"]


def _pin(c, h, sid, reason="核对口径"):
    return c.post(f"/api/slices/{sid}/lock", headers=h, json={"reason": reason})


def test_spec_pinned_to_plan_jia(client):
    h = _h(client)
    sp = client.get("/api/boundary/spec", headers=h).json()
    assert sp["plan"] == "甲"
    assert "作废昨日" in sp["plan_desc"]
    assert len(sp["rules"]) >= 5
    # 未登录访问日界接口一律 401，会话登录照用
    assert client.get("/api/boundary/state").status_code == 401


def test_same_day_lock_blocks_release_and_writes_full_trail(client):
    h = _h(client)
    sid = _complete_slice(client, h)
    r = _pin(client, h, sid)
    assert r.status_code == 200
    lock_id = r.json()["lock_id"]
    assert r.json()["biz_date"] == "2026-11-02"

    rel = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 409
    assert f"锁#{lock_id}" in rel.json()["detail"]

    # 成功/拒绝流水留痕
    rels = client.get("/api/releases", headers=h).json()["items"]
    row = next(x for x in rels if x["slice_id"] == sid)
    assert row["status"] == "拒绝" and f"锁#{lock_id}" in row["note"]
    # 审计留痕
    evs = [a["event_type"] for a in client.get("/api/boundary/audit", headers=h).json()["items"]]
    assert "策略钉死" in evs and "放行拒绝" in evs
    # 重复钉策略被拒
    assert _pin(client, h, sid).status_code == 409


def test_day_cut_voids_yesterday_locks_and_repin_required(client):
    h = _h(client)
    sid = _complete_slice(client, h)
    lock_id = _pin(client, h, sid).json()["lock_id"]
    assert client.post(f"/api/slices/{sid}/release", headers=h,
                       json={"consumer": "ads_x"}).status_code == 409

    cut = client.post("/api/boundary/day-cut", headers=h, json={})
    assert cut.status_code == 200
    assert cut.json() == {"ok": True, "from": "2026-11-02", "to": "2026-11-03",
                          "voided_locks": 1, "plan": "甲"}

    # 昨锁已作废，不再拦今日放行（无需结转）
    rel = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 200
    assert rel.json()["cleaned_residue_locks"] == []

    st = client.get("/api/boundary/state", headers=h).json()
    assert st["current_biz_date"] == "2026-11-03"
    assert st["active_count"] == 0 and st["residue_count"] == 0
    lock = next(l for l in st["locks"] if l["id"] == lock_id)
    assert lock["status"] == "作废" and "甲方案" in lock["void_reason"]
    # 日切流水
    assert st["day_cuts"][0]["voided_locks"] == 1

    # 次日必须重新钉策略，新锁业务日为新业务日
    again = _pin(client, h, sid, "次日新策略")
    assert again.status_code == 200 and again.json()["biz_date"] == "2026-11-03"
    assert client.post(f"/api/slices/{sid}/release", headers=h,
                       json={"consumer": "ads_x"}).status_code == 409


def test_carryover_rejected_and_deviation_registered(client):
    h = _h(client)
    sid = _complete_slice(client, h)
    lock_id = _pin(client, h, sid).json()["lock_id"]

    r = client.post(f"/api/boundary/carryover/{lock_id}", headers=h, json={})
    assert r.status_code == 409 and "日界口径偏差" in r.json()["detail"]

    # 锁原封不动，偏差计数 +1，审计可查
    st = client.get("/api/boundary/state", headers=h).json()
    assert st["deviation_count"] == 1
    assert next(l for l in st["locks"] if l["id"] == lock_id)["status"] == "生效"
    dev = [a for a in client.get("/api/boundary/audit", headers=h).json()["items"]
           if a["event_type"] == "日界口径偏差"]
    assert len(dev) == 1 and "结转" in dev[0]["payload"]


def test_backdated_and_illegal_day_cut_rejected(client):
    h = _h(client)
    for bad in ["2026-11-02", "2026-11-01"]:
        r = client.post("/api/boundary/day-cut", headers=h, json={"to_biz_date": bad})
        assert r.status_code == 409 and "日界口径偏差" in r.json()["detail"]
    bad_fmt = client.post("/api/boundary/day-cut", headers=h, json={"to_biz_date": "11/03"})
    assert bad_fmt.status_code == 409

    # 业务日未被改动，偏差全部登记
    st = client.get("/api/boundary/state", headers=h).json()
    assert st["current_biz_date"] == "2026-11-02" and st["deviation_count"] == 3
    assert st["day_cuts"] == []


def test_residue_lock_from_prior_day_never_blocks_release(client):
    h = _h(client)
    sid = _complete_slice(client, h)
    # 模拟历史版本遗留：日切后仍挂着一笔昨日生效锁
    client.post("/api/boundary/day-cut", headers=h, json={"to_biz_date": "2026-11-03"})
    with day_boundary.connect() as conn:
        conn.execute(
            "INSERT INTO policy_locks(slice_id,biz_date,reason,status,pinned_by,pinned_at)"
            " VALUES(?,?,?,'生效',?,?)",
            (sid, "2026-11-02", "昨日遗留锁", "legacy", "2026-11-02T23:59:00"))
        conn.commit()

    st = client.get("/api/boundary/state", headers=h).json()
    assert st["residue_count"] == 1

    rel = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 200
    cleaned = rel.json()["cleaned_residue_locks"]
    assert len(cleaned) == 1
    assert "跨日锁定残留1笔" in rel.json()["note"]

    # 残留锁已作废且有清理审计，策略台/关日台残留读数归零
    st2 = client.get("/api/boundary/state", headers=h).json()
    assert st2["residue_count"] == 0
    evs = client.get("/api/boundary/audit", headers=h).json()["items"]
    assert any(a["event_type"] == "跨日残留清理" for a in evs)
    rel_note = next(x for x in client.get("/api/releases", headers=h).json()["items"]
                    if x["slice_id"] == sid)["note"]
    assert "残留" in rel_note  # 成功流水与服务端备注一致


def test_day_cut_and_pin_are_mutex_loser_records_contention(client):
    h = _h(client)
    sid = _complete_slice(client, h)
    # 主线程先占住日界互斥量，模拟日切正在进行；定时释放让后手能补登争用
    day_boundary._lock.acquire()
    timer = threading.Timer(0.25, day_boundary._lock.release)
    timer.start()
    try:
        r = _pin(client, h, sid)
    finally:
        timer.join()
    assert r.status_code == 409 and "日界争用" in r.json()["detail"]

    st = client.get("/api/boundary/state", headers=h).json()
    assert st["contention_count"] == 1
    ev = next(a for a in client.get("/api/boundary/audit", headers=h).json()["items"]
              if a["event_type"] == "日界争用")
    assert "策略钉死" in ev["payload"]
    # 被拒动作未落任何业务锁
    assert st["active_count"] == 0


def test_five_views_share_one_reading(client):
    h = _h(client)
    sid = _complete_slice(client, h, no="SL-CONS-1")
    _pin(client, h, sid, reason="对账冻结")
    client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})

    state = client.get("/api/boundary/state", headers=h).json()          # 策略台/关日台
    listing = client.get("/api/slices", headers=h).json()                # 目录旁注
    detail = client.get(f"/api/slices/{sid}/boundary", headers=h).json()  # 详情旁注
    rels = client.get("/api/releases", headers=h).json()["items"]        # 成功流水
    audits = client.get("/api/boundary/audit", headers=h).json()["items"]

    today = state["current_biz_date"]
    row = next(s for s in listing["items"] if s["id"] == sid)
    # 目录旁注与详情旁注同源同文
    assert row["boundary_note"] == detail["boundary_note"]
    assert row["current_biz_date"] == detail["current_biz_date"] == today
    assert row["boundary_locked"] is True and detail["active_lock"]["reason"] == "对账冻结"
    # 策略台与关日台同一接口同一读数
    assert state["active_count"] == 1
    # 成功/拒绝流水里锁定原因与锁号一致
    rejected = next(x for x in rels if x["slice_id"] == sid)
    assert rejected["status"] == "拒绝" and "对账冻结" in rejected["note"]
    # 审计链完整
    kinds = {a["event_type"] for a in audits}
    assert {"策略钉死", "放行拒绝"} <= kinds


def test_successful_release_writes_audit_and_watermark(client):
    h = _h(client)
    sid = _complete_slice(client, h, no="SL-OK-1")
    r = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_ok"})
    assert r.status_code == 200
    wm = client.get("/api/watermarks", headers=h).json()["items"]
    assert any(w["source_slice"] == "SL-OK-1" and w["consumer"] == "ads_ok" for w in wm)
    evs = client.get("/api/boundary/audit", headers=h).json()["items"]
    assert any(a["event_type"] == "放行成功" for a in evs)


def test_manual_void_writes_audit_and_frees_release(client):
    h = _h(client)
    sid = _complete_slice(client, h, no="SL-V-1")
    lock_id = _pin(client, h, sid).json()["lock_id"]
    r = client.post(f"/api/boundary/locks/{lock_id}/void", headers=h,
                    json={"reason": "人工解锁"})
    assert r.status_code == 200
    assert client.post(f"/api/slices/{sid}/release", headers=h,
                       json={"consumer": "ads_v"}).status_code == 200
    evs = client.get("/api/boundary/audit", headers=h).json()["items"]
    assert any(a["event_type"] == "锁定作废" for a in evs)
