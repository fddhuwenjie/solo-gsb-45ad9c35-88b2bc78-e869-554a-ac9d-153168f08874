"""陶瓷合窑装载确认台 — 规则测试。"""
import os
import tempfile
import threading

import pytest

# 避免模块级默认实例污染工作目录
os.environ.setdefault("KILN_DB", os.path.join(tempfile.mkdtemp(), "import-default.db"))

import app as kiln_app


@pytest.fixture()
def app(tmp_path):
    return kiln_app.create_app(str(tmp_path / "test.db"))


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------- 辅助 ----------

def make_batch(client, no, pmin, pmax, smin, smax):
    r = client.post("/api/batches", json={
        "batch_no": no, "peak_min": pmin, "peak_max": pmax,
        "soak_min": smin, "soak_max": smax,
    })
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def get_draft(client, kiln_id):
    r = client.get(f"/api/kilns/{kiln_id}/draft")
    assert r.status_code == 200
    return r.get_json()


def put(client, draft_id, pos, batch_id):
    r = client.put(f"/api/drafts/{draft_id}/items/{pos}", json={"batch_id": batch_id})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def preview(client, draft_id):
    r = client.post(f"/api/drafts/{draft_id}/preview")
    assert r.status_code == 200
    return r.get_json()


def confirm(client, draft_id, pv):
    return client.post(f"/api/drafts/{draft_id}/confirm", json={
        "draft_version": pv["draft_version"],
        "batch_versions": pv["batch_versions"],
    })


def kiln_ids(client):
    return [k["id"] for k in client.get("/api/kilns").get_json()]


# ---------- 批次登记 ----------

def test_batch_create_and_validation(client):
    b = make_batch(client, "B1", 1200, 1280, 30, 60)
    assert b["version"] == 1 and b["used_by"] is None

    r = client.post("/api/batches", json={
        "batch_no": "B2", "peak_min": 1300, "peak_max": 1200,
        "soak_min": 10, "soak_max": 20})
    assert r.status_code == 400 and "峰温" in r.get_json()["error"]

    r = client.post("/api/batches", json={
        "batch_no": "B1", "peak_min": 1, "peak_max": 2, "soak_min": 1, "soak_max": 2})
    assert r.status_code == 409  # 编号重复


def test_batch_update_bumps_version(client):
    b = make_batch(client, "B1", 1200, 1280, 30, 60)
    r = client.put(f"/api/batches/{b['id']}", json={
        "batch_no": "B1", "peak_min": 1210, "peak_max": 1280,
        "soak_min": 30, "soak_max": 60})
    assert r.status_code == 200
    assert r.get_json()["version"] == 2


# ---------- 草案与预览 ----------

def test_draft_preview_common_interval_and_occupancy(client):
    k1, _k2 = kiln_ids(client)
    b1 = make_batch(client, "B1", 1200, 1280, 30, 60)
    b2 = make_batch(client, "B2", 1240, 1300, 45, 90)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    d = put(client, d["id"], 3, b2["id"])
    assert d["version"] == 3  # 建草案 v1 + 两次占位

    pv = preview(client, d["id"])
    assert pv["ok"] is True
    assert pv["common_peak"] == [1240.0, 1280.0]
    assert pv["common_soak"] == [45, 60]
    occ = {o["position_no"]: o["batch_no"] for o in pv["occupancy"]}
    assert occ[1] == "B1" and occ[3] == "B2" and occ[2] is None
    assert pv["batch_versions"] == {str(b1["id"]): 1, str(b2["id"]): 1}


def test_preview_reports_no_intersection_with_batch_nos(client):
    k1, _ = kiln_ids(client)
    b1 = make_batch(client, "LOW", 1100, 1150, 30, 60)
    b2 = make_batch(client, "HIGH", 1300, 1350, 30, 60)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    put(client, d["id"], 2, b2["id"])
    pv = preview(client, d["id"])
    assert pv["ok"] is False
    assert pv["common_peak"] is None
    msg = " ".join(pv["problems"])
    assert "LOW" in msg and "HIGH" in msg


def test_one_batch_one_position_and_invalid_position(client):
    k1, _ = kiln_ids(client)
    b1 = make_batch(client, "B1", 1200, 1280, 30, 60)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    d = put(client, d["id"], 2, b1["id"])  # 同一批次移动窑位
    positions = [(it["position_no"], it["batch_no"]) for it in d["items"]]
    assert positions == [(2, "B1")]

    r = client.put(f"/api/drafts/{d['id']}/items/99", json={"batch_id": b1["id"]})
    assert r.status_code == 400 and "窑位 99" in r.get_json()["error"]


# ---------- 确认：成功与不可变快照 ----------

def test_confirm_success_saves_immutable_snapshot(client):
    k1, _ = kiln_ids(client)
    b1 = make_batch(client, "B1", 1200, 1280, 30, 60)
    b2 = make_batch(client, "B2", 1240, 1300, 45, 90)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    put(client, d["id"], 2, b2["id"])
    pv = preview(client, d["id"])

    r = confirm(client, d["id"], pv)
    assert r.status_code == 201, r.get_json()
    load = r.get_json()
    assert load["run_no"] == "R0001"
    assert load["peak_min"] == 1240.0 and load["peak_max"] == 1280.0
    assert load["soak_min"] == 45 and load["soak_max"] == 60
    assert [(i["position_no"], i["batch_no"]) for i in load["items"]] == [(1, "B1"), (2, "B2")]

    # 草案已清空且版本提升；批次标记为已使用
    d2 = get_draft(client, k1)
    assert d2["items"] == [] and d2["version"] > pv["draft_version"]
    used = {b["batch_no"]: b["used_by"] for b in client.get("/api/batches").get_json()}
    assert used["B1"] == "R0001" and used["B2"] == "R0001"

    # 已确认批次禁止修改与删除
    r = client.put(f"/api/batches/{b1['id']}", json={
        "batch_no": "B1", "peak_min": 1, "peak_max": 2, "soak_min": 1, "soak_max": 2})
    assert r.status_code == 409
    r = client.delete(f"/api/batches/{b1['id']}")
    assert r.status_code == 409

    # 快照保持确认时的取值
    snap = client.get("/api/loads").get_json()[0]
    it = next(i for i in snap["items"] if i["batch_no"] == "B1")
    assert (it["peak_min"], it["peak_max"]) == (1200.0, 1280.0)


# ---------- 确认：过期预览 ----------

def test_confirm_rejected_when_draft_changed_after_preview(client):
    k1, _ = kiln_ids(client)
    b1 = make_batch(client, "B1", 1200, 1280, 30, 60)
    b2 = make_batch(client, "B2", 1210, 1270, 30, 60)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    pv = preview(client, d["id"])
    put(client, d["id"], 2, b2["id"])  # 预览后有人改了窑位占用

    r = confirm(client, d["id"], pv)
    assert r.status_code == 409
    assert r.get_json()["code"] == "STALE_DRAFT"
    assert "刷新" in r.get_json()["error"]
    assert client.get("/api/loads").get_json() == []  # 未产生任何确认记录

    r = confirm(client, d["id"], preview(client, d["id"]))  # 刷新预览后可确认
    assert r.status_code == 201


def test_confirm_rejected_when_batch_range_changed_after_preview(client):
    k1, _ = kiln_ids(client)
    b1 = make_batch(client, "B1", 1200, 1280, 30, 60)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    pv = preview(client, d["id"])
    client.put(f"/api/batches/{b1['id']}", json={  # 预览后有人改了工艺范围
        "batch_no": "B1", "peak_min": 1250, "peak_max": 1280,
        "soak_min": 30, "soak_max": 60})

    r = confirm(client, d["id"], pv)
    body = r.get_json()
    assert r.status_code == 409 and body["code"] == "STALE_BATCH"
    assert "B1" in body["error"] and "刷新" in body["error"]
    assert client.get("/api/loads").get_json() == []


# ---------- 确认：业务校验失败且无副作用 ----------

def test_confirm_no_intersection_points_out_batches_and_keeps_state(client):
    k1, _ = kiln_ids(client)
    b1 = make_batch(client, "LOW", 1100, 1150, 30, 60)
    b2 = make_batch(client, "HIGH", 1300, 1350, 30, 60)
    d = get_draft(client, k1)
    put(client, d["id"], 1, b1["id"])
    put(client, d["id"], 2, b2["id"])
    pv = preview(client, d["id"])

    r = confirm(client, d["id"], pv)
    body = r.get_json()
    assert r.status_code == 409 and body["code"] == "NO_INTERSECTION_PEAK"
    assert "LOW" in body["error"] and "HIGH" in body["error"]

    # 草案与已确认记录均未改变
    d2 = get_draft(client, k1)
    assert len(d2["items"]) == 2 and d2["version"] == pv["draft_version"]
    assert client.get("/api/loads").get_json() == []


def test_batch_cannot_be_used_by_two_confirmed_loads(client):
    k1, k2 = kiln_ids(client)
    b1 = make_batch(client, "B1", 1200, 1280, 30, 60)
    d1 = get_draft(client, k1)
    d2 = get_draft(client, k2)
    put(client, d1["id"], 1, b1["id"])
    put(client, d2["id"], 1, b1["id"])  # 两个草案争用同一批次

    r1 = confirm(client, d1["id"], preview(client, d1["id"]))
    assert r1.status_code == 201

    r2 = confirm(client, d2["id"], preview(client, d2["id"]))
    body = r2.get_json()
    assert r2.status_code == 409 and body["code"] == "BATCH_USED"
    assert "B1" in body["error"] and "R0001" in body["error"]

    # 第二个草案保持原样，且只有一条确认记录
    assert len(get_draft(client, k2)["items"]) == 1
    assert len(client.get("/api/loads").get_json()) == 1

    # 已确认批次也不能再被放入新草案
    r = client.put(f"/api/drafts/{d2['id']}/items/2", json={"batch_id": b1["id"]})
    assert r.status_code == 409


def test_concurrent_confirm_only_one_wins(app):
    c = app.test_client()
    k1, k2 = kiln_ids(c)
    b1 = make_batch(c, "B1", 1200, 1280, 30, 60)
    d1 = get_draft(c, k1)
    d2 = get_draft(c, k2)
    put(c, d1["id"], 1, b1["id"])
    put(c, d2["id"], 1, b1["id"])
    pv1 = preview(c, d1["id"])
    pv2 = preview(c, d2["id"])

    results = []

    def worker(draft_id, pv):
        cc = app.test_client()
        r = confirm(cc, draft_id, pv)
        results.append((r.status_code, (r.get_json() or {}).get("code")))

    t1 = threading.Thread(target=worker, args=(d1["id"], pv1))
    t2 = threading.Thread(target=worker, args=(d2["id"], pv2))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(s for s, _ in results)
    assert statuses == [201, 409]
    loser_code = next(code for s, code in results if s == 409)
    assert loser_code == "BATCH_USED"
    assert len(c.get("/api/loads").get_json()) == 1


# ---------- 持久化 ----------

def test_data_survives_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    app1 = kiln_app.create_app(db)
    c1 = app1.test_client()
    k1, _ = kiln_ids(c1)
    b1 = make_batch(c1, "B1", 1200, 1280, 30, 60)
    d = get_draft(c1, k1)
    put(c1, d["id"], 1, b1["id"])
    assert confirm(c1, d["id"], preview(c1, d["id"])).status_code == 201

    app2 = kiln_app.create_app(db)  # 模拟重启
    c2 = app2.test_client()
    loads = c2.get("/api/loads").get_json()
    assert len(loads) == 1 and loads[0]["run_no"] == "R0001"
    assert loads[0]["items"][0]["batch_no"] == "B1"
    batches = c2.get("/api/batches").get_json()
    assert batches[0]["used_by"] == "R0001"
