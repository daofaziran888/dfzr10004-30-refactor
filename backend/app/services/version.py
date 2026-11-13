"""采集配置版本业务规则：生命周期由共用状态机驱动，状态写入与审计留痕同事务。

发布、回滚、废弃不再各自解释下一状态：迁移规则只在 ``LIFECYCLE`` 声明一份，
列表、单条详情、审计留痕读到的都是同一份结果。每次成功迁移与审计事件在
``store.transaction`` 里一起落库；失败动作不改变版本，重复动作不追加审计。
"""
from __future__ import annotations

from typing import Any

from app.services import audit as audit_module
from app.services.audit import audit_service
from app.services.lifecycle import Lifecycle, Transition
from app.store import store

MODULE = "version"
REQUIRED_FIELDS = ["版本编号", "配置名称", "版本号"]

# 版本状态迁移规则只有这一份：保留 草稿→已发布→已回滚→已废弃 的顺序约束。
LIFECYCLE = Lifecycle(
    label="采集配置版本",
    states=["草稿", "已发布", "已回滚", "已废弃"],
    transitions=[
        Transition("发布版本", "草稿", "已发布"),
        Transition("回滚版本", "已发布", "已回滚", negative=True),
        Transition("废弃版本", "已回滚", "已废弃"),
    ],
    # 旧动作名称兼容映射也只有这一份，各组调用归一后共用同一结果。
    aliases={"发布": "发布版本", "回滚": "回滚版本", "废弃": "废弃版本"},
)


class VersionService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.locked():
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("版本编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [self._present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.locked():
            entry = store.find(MODULE, entry_id)
            return self._present(entry) if entry is not None else None

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        """单条详情：版本字段与审计留痕在同一把锁里读出，和审计清单是同一份结果。"""
        with store.locked():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            detail = self._present(entry)
            detail["audit_trail"] = [
                dict(row) for row in audit_service.list_for_version(entry.get("版本编号"))
            ]
            return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction(MODULE):
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = LIFECYCLE.states[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return self._present(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        base_status: str | None = None,
        operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """执行一次状态迁移：校验、写状态、写审计在同一个事务边界里完成。

        ``base_status`` 是调用方读取页面时看到的状态，用于并发场景的乐观校验：
        状态已被别的请求推进过时，本次动作整体失败，不留下半边事件。
        """
        canonical = LIFECYCLE.canonical_action(action)
        if canonical is None:
            return None, None, f"动作「{action}」不属于采集配置版本可执行范围"
        with store.transaction(MODULE, audit_module.MODULE):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, None, f"配置版本 {entry_id} 不存在或已归档"
            self._normalize(entry)
            expected = str(base_status or "").strip()
            if expected and expected != entry["status"]:
                return None, None, f"配置版本状态已变为「{entry['status']}」，与页面基于的「{expected}」不一致，请刷新后重试"
            transition, error = LIFECYCLE.evaluate(entry["status"], canonical)
            if error:
                return None, None, error
            LIFECYCLE.apply(entry, transition)
            event = audit_service.record_version_event(entry, transition, operator=operator)
            audit_service.refresh_version_queue(
                entry.get("版本编号"), entry["status"], terminal=LIFECYCLE.terminal
            )
        return self._present(entry), event, f"配置版本已{canonical}，审计记录 {event['记录编号']} 已留痕"

    def _present(self, entry: dict[str, Any]) -> dict[str, Any]:
        """对外展示的版本行：结构字段补齐 + 可执行动作，列表与单条读同一份规则。"""
        self._normalize(entry)
        row = dict(entry)
        row["available_actions"] = LIFECYCLE.available_actions(row["status"])
        return row

    @staticmethod
    def _normalize(entry: dict[str, Any]) -> None:
        """历史行缺结构字段时按原结论补齐：只补 status/pending/abnormal，不改写业务字段。"""
        status = entry.get("status")
        if status not in LIFECYCLE.states:
            legacy = str(entry.get("发布状态") or "").strip()
            entry["status"] = legacy if legacy in LIFECYCLE.states else LIFECYCLE.states[0]
        entry.setdefault("pending", entry["status"] != LIFECYCLE.terminal)
        entry.setdefault("abnormal", False)


version_service = VersionService()
