"""采集合规配置业务规则：状态流转、字段校验与筛选口径都收在这里，迁移规则由共用状态机解释。"""
from __future__ import annotations

from typing import Any

from app.services.lifecycle import Lifecycle, Transition
from app.store import store

MODULE = "compliance"
REQUIRED_FIELDS = ['规则编号', '站点域名', '协议标识']

LIFECYCLE = Lifecycle(
    label="采集合规配置",
    states=['草稿', '待审批', '已生效', '已废弃'],
    transitions=[
        Transition("提交审批", "草稿", "待审批"),
        Transition("发布规则", "待审批", "已生效"),
        Transition("废弃规则", "已生效", "已废弃"),
    ],
)


class ComplianceService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
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
                return None, f"合规规则 {entry_id} 不存在或已归档"
            transition, error = LIFECYCLE.evaluate(str(entry.get("status") or ""), action)
            if error:
                return None, error
            LIFECYCLE.apply(entry, transition)
        return entry, f"合规规则已{transition.action}"
