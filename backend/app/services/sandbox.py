"""采集沙箱测试业务规则：状态流转、字段校验与筛选口径都收在这里，迁移规则由共用状态机解释。"""
from __future__ import annotations

from typing import Any

from app.services.lifecycle import Lifecycle, Transition
from app.store import store

MODULE = "sandbox"
REQUIRED_FIELDS = ['实例编号', '测试脚本', '目标站点']

LIFECYCLE = Lifecycle(
    label="采集沙箱测试",
    states=['草稿', '运行中', '已完成', '已关闭'],
    transitions=[
        Transition("启动测试", "草稿", "运行中"),
        Transition("关闭实例", "运行中", "已关闭"),
        Transition("归档实例", "已完成", "已关闭"),
    ],
)


class SandboxService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("实例编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = LIFECYCLE.states[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction(MODULE):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"沙箱实例 {entry_id} 不存在或已归档"
            transition, error = LIFECYCLE.evaluate(str(entry.get("status") or ""), action)
            if error:
                return None, error
            LIFECYCLE.apply(entry, transition)
        return entry, f"沙箱实例已{transition.action}"
