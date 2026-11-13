"""版本生命周期结构验证：一份状态机、同事务留痕、并发只留一个结果。

运行方式：cd backend && .venv/bin/python -m pytest tests/ -v

测试通过真实 uvicorn 实例发请求，并发用例用多线程同时打同一个版本，
验证「最终只有一个状态和一个审计事件」。
"""
from __future__ import annotations

import concurrent.futures
import itertools
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import app
from app.store import store

HOST = "127.0.0.1"
PORT = 8123
_version_seq = itertools.count(9001)


@pytest.fixture(scope="session")
def server():
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="error")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    for _ in range(100):
        if instance.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("测试服务启动超时")
    yield f"http://{HOST}:{PORT}"
    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def client(server):
    with httpx.Client(base_url=server, timeout=10) as http:
        yield http


def create_version(client: httpx.Client) -> dict:
    no = f"VERS-{next(_version_seq):04d}"
    resp = client.post("/api/version", json={
        "values": {"版本编号": no, "配置名称": f"生命周期验证配置-{no}", "版本号": "v1.0"},
    })
    payload = resp.json()
    assert payload["ok"], payload
    return payload["entry"]


def run_action(client: httpx.Client, entry_id: int, action: str, base_status: str | None = None) -> dict:
    values: dict[str, str] = {"action": action}
    if base_status is not None:
        values["base_status"] = base_status
    return client.post(f"/api/version/{entry_id}/actions", json={"values": values}).json()


def audit_trail(client: httpx.Client, version_no: str) -> list[dict]:
    return client.get("/api/audit", params={"version_no": version_no}).json()["items"]


def test_seeded_versions_keep_original_conclusions(client):
    """迁移后 VERS-0001 至 VERS-0003 按原结论可读，历史审计不被补造。"""
    page = client.get("/api/version", params={"keyword": "VERS-000"}).json()
    assert page["total"] == 3
    by_no = {row["版本编号"]: row for row in page["items"]}
    assert by_no["VERS-0001"]["status"] == "草稿"
    assert by_no["VERS-0002"]["status"] == "已发布"
    assert by_no["VERS-0003"]["status"] == "已回滚"
    # 列表与单条读同一份状态机：可执行动作一致
    assert by_no["VERS-0001"]["available_actions"] == ["发布版本"]
    assert by_no["VERS-0002"]["available_actions"] == ["回滚版本"]
    assert by_no["VERS-0003"]["available_actions"] == ["废弃版本"]
    for entry_id in (1, 2, 3):
        detail = client.get(f"/api/version/{entry_id}").json()
        assert detail["audit_trail"] == []


def test_full_lifecycle_writes_one_audit_per_transition(client):
    """草稿→已发布→已回滚→已废弃：每步一条审计，终态后待办清零、动作全拒。"""
    entry = create_version(client)
    vid, vno = entry["id"], entry["版本编号"]
    assert entry["status"] == "草稿"

    for action, source, target in [("发布版本", "草稿", "已发布"), ("回滚版本", "已发布", "已回滚"), ("废弃版本", "已回滚", "已废弃")]:
        result = run_action(client, vid, action, base_status=source)
        assert result["ok"], result
        assert result["entry"]["status"] == target
        assert result["audit"]["操作类型"] == action
        assert result["audit"]["版本编号"] == vno
        assert source in result["audit"]["变更内容"] and target in result["audit"]["变更内容"]
        if target != "已废弃":
            assert all(row["pending"] for row in audit_trail(client, vno))

    trail = audit_trail(client, vno)
    assert [row["操作类型"] for row in trail] == ["发布版本", "回滚版本", "废弃版本"]
    assert all(row["记录编号"].startswith("AUDI-") for row in trail)
    # 审计待办队列跟着版本状态刷新：版本终态后关联待办清零
    assert all(not row["pending"] for row in trail)
    detail = client.get(f"/api/version/{vid}").json()
    assert detail["available_actions"] == []
    again = run_action(client, vid, "回滚版本")
    assert not again["ok"]
    assert len(audit_trail(client, vno)) == 3


def test_failed_action_changes_nothing(client):
    """失败动作不改变版本，也不追加审计。"""
    entry = create_version(client)
    vid, vno = entry["id"], entry["版本编号"]
    for action in ("回滚版本", "废弃版本", "不存在的动作", ""):
        result = run_action(client, vid, action)
        assert not result["ok"], (action, result)
    detail = client.get(f"/api/version/{vid}").json()
    assert detail["status"] == "草稿"
    assert audit_trail(client, vno) == []


def test_repeated_action_does_not_append_audit(client):
    """重复动作不追加审计。"""
    entry = create_version(client)
    vid, vno = entry["id"], entry["版本编号"]
    first = run_action(client, vid, "发布版本")
    assert first["ok"]
    second = run_action(client, vid, "发布版本")
    assert not second["ok"]
    assert len(audit_trail(client, vno)) == 1


def test_concurrent_publish_and_rollback_leave_single_state_and_single_audit(client):
    """并发发布与回滚序列：每一轮只有一个胜者，最终只有一个状态和一个审计事件。"""
    entry = create_version(client)
    vid, vno = entry["id"], entry["版本编号"]

    def fire(actions: list[str], base_status: str) -> list[dict]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = [pool.submit(run_action, client, vid, action, base_status) for action in actions]
            return [future.result() for future in futures]

    # 第一轮：5 个发布 + 5 个回滚同时打到草稿版本上，恰好一个发布生效
    results = fire(["发布版本"] * 5 + ["回滚版本"] * 5, "草稿")
    winners = [r for r in results if r["ok"]]
    assert len(winners) == 1, results
    assert winners[0]["audit"]["操作类型"] == "发布版本"
    detail = client.get(f"/api/version/{vid}").json()
    assert detail["status"] == "已发布"
    assert len(detail["audit_trail"]) == 1

    # 第二轮：5 个回滚 + 5 个发布，恰好一个回滚生效
    results = fire(["回滚版本"] * 5 + ["发布版本"] * 5, "已发布")
    winners = [r for r in results if r["ok"]]
    assert len(winners) == 1, results
    assert winners[0]["audit"]["操作类型"] == "回滚版本"
    detail = client.get(f"/api/version/{vid}").json()
    assert detail["status"] == "已回滚"
    assert len(detail["audit_trail"]) == 2

    # 第三轮：5 个废弃 + 5 个回滚，恰好一个废弃生效
    results = fire(["废弃版本"] * 5 + ["回滚版本"] * 5, "已回滚")
    winners = [r for r in results if r["ok"]]
    assert len(winners) == 1, results
    assert winners[0]["audit"]["操作类型"] == "废弃版本"
    detail = client.get(f"/api/version/{vid}").json()
    assert detail["status"] == "已废弃"
    assert len(detail["audit_trail"]) == 3

    # 终态后并发再打也全部失败，审计不再增加
    results = fire(["发布版本", "回滚版本", "废弃版本"] * 3, "已废弃")
    assert not any(r["ok"] for r in results)
    assert len(audit_trail(client, vno)) == 3


def test_legacy_action_aliases_share_same_result(client):
    """旧动作名称走同一份兼容映射，留痕用归一后的动作名。"""
    entry = create_version(client)
    vid, vno = entry["id"], entry["版本编号"]
    for alias, canonical, target in [("发布", "发布版本", "已发布"), ("回滚", "回滚版本", "已回滚"), ("废弃", "废弃版本", "已废弃")]:
        result = run_action(client, vid, alias)
        assert result["ok"], result
        assert result["entry"]["status"] == target
        assert result["audit"]["操作类型"] == canonical
    assert [row["操作类型"] for row in audit_trail(client, vno)] == ["发布版本", "回滚版本", "废弃版本"]


def test_audit_list_and_version_detail_read_same_result(client):
    """审计记录清单与版本详情读取同一份结果。"""
    entry = create_version(client)
    vid, vno = entry["id"], entry["版本编号"]
    run_action(client, vid, "发布版本")
    run_action(client, vid, "回滚版本")
    detail = client.get(f"/api/version/{vid}").json()
    listing = audit_trail(client, vno)
    assert len(listing) == 2
    assert [row["记录编号"] for row in detail["audit_trail"]] == [row["记录编号"] for row in listing]


def test_historical_rows_missing_fields_normalized(client):
    """历史缺失字段按原结论补齐：旧「发布状态」可识别，缺状态落到序列起点。"""
    store.rows("version").append({"id": 9901, "版本编号": "VERS-9901", "配置名称": "历史遗留版本", "版本号": "v0.9", "发布状态": "已发布"})
    store.rows("version").append({"id": 9902, "版本编号": "VERS-9902", "配置名称": "更老的版本", "版本号": "v0.1"})
    try:
        first = client.get("/api/version/9901").json()
        assert first["status"] == "已发布"
        assert first["available_actions"] == ["回滚版本"]
        second = client.get("/api/version/9902").json()
        assert second["status"] == "草稿"
        assert second["available_actions"] == ["发布版本"]
        result = run_action(client, 9901, "回滚版本")
        assert result["ok"]
        assert result["entry"]["status"] == "已回滚"
    finally:
        store._tables["version"] = [row for row in store.rows("version") if row.get("id") not in (9901, 9902)]
        store._tables["audit"] = [row for row in store.rows("audit") if row.get("版本编号") not in ("VERS-9901", "VERS-9902")]


def test_create_validation(client):
    resp = client.post("/api/version", json={"values": {"配置名称": "缺编号"}}).json()
    assert not resp["ok"]
    assert "版本编号" in resp["message"]
