"""采集审计日志业务规则：审计自身的状态流转，以及版本迁移留痕的统一读写口径。

版本模块的每次状态迁移都通过 ``record_version_event`` 留痕，并且和状态写入
处在同一个事务边界里；审计清单与版本详情都走 ``list_for_version`` 这一个读口径，
两边读到的必然是同一份结果。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.lifecycle import Lifecycle, Transition
from app.store import store

MODULE = "audit"
REQUIRED_FIELDS = ["记录编号", "操作类型", "采集计划"]
RECORD_PREFIX = "AUDI"

# 审计自身的生命周期也只声明这一份，迁移规则由共用状态机解释。
LIFECYCLE = Lifecycle(
    label="采集审计日志",
    states=["已记录", "已复核", "已归档"],
    transitions=[
        Transition("标记复核", "已记录", "已复核"),
        Transition("归档记录", "已复核", "已归档"),
    ],
)


class AuditService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if version_no:
            rows = [row for row in rows if self._matches_version(row, version_no)]
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
                return None, f"审计记录 {entry_id} 不存在或已归档"
            transition, error = LIFECYCLE.evaluate(str(entry.get("status") or ""), action)
            if error:
                return None, error
            LIFECYCLE.apply(entry, transition)
        return entry, f"审计记录已{transition.action}"

    # ---- 版本状态迁移的审计留痕：与版本状态写入同一个事务边界 ----

    def record_version_event(
        self,
        version_row: dict[str, Any],
        transition: Transition,
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """为一次版本状态迁移追加一条审计记录；编号延续 AUDI 序列，历史不补造。"""
        rows = store.rows(MODULE)
        event = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": LIFECYCLE.states[0],
            "pending": True,
            "abnormal": transition.negative,
            "记录编号": self._next_record_no(rows),
            "操作类型": transition.action,
            "采集计划": str(version_row.get("配置名称") or "—"),
            "执行人": operator or "系统",
            "执行时间": date.today().isoformat(),
            "结果摘要": f"配置版本{transition.action}：{transition.source} → {transition.target}",
            "变更内容": f"{version_row.get('版本编号')} 状态由「{transition.source}」迁移为「{transition.target}」",
            "复核状态": "待复核",
            "版本编号": version_row.get("版本编号"),
        }
        rows.append(event)
        return event

    def list_for_version(self, version_no: str | None) -> list[dict[str, Any]]:
        """版本维度的审计清单：审计列表与版本详情共用这一个读口径。"""
        return [row for row in store.rows(MODULE) if self._matches_version(row, version_no)]

    def refresh_version_queue(self, version_no: str | None, version_status: str, *, terminal: str) -> None:
        """审计待办队列跟着版本状态刷新：版本未终态时关联记录保持待办，走到终态自动清零。"""
        still_open = version_status != terminal
        for row in store.rows(MODULE):
            if self._matches_version(row, version_no):
                row["pending"] = still_open and row.get("status") != LIFECYCLE.terminal

    @staticmethod
    def _matches_version(row: dict[str, Any], version_no: str | None) -> bool:
        return bool(version_no) and str(row.get("版本编号") or "") == str(version_no)

    @staticmethod
    def _next_record_no(rows: list[dict[str, Any]]) -> str:
        sequence = 0
        for row in rows:
            number = str(row.get("记录编号") or "")
            prefix = f"{RECORD_PREFIX}-"
            if number.startswith(prefix):
                try:
                    sequence = max(sequence, int(number[len(prefix):]))
                except ValueError:
                    continue
        return f"{RECORD_PREFIX}-{sequence + 1:04d}"


audit_service = AuditService()
