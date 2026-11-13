"""采集配置版本接口：维护配置版本，覆盖发布版本、回滚版本、废弃版本等动作。

生命周期规则只在 services.version.LIFECYCLE 声明一份；这里的状态常量也从
同一份规则派生，不再单独解释。状态写入与审计留痕在服务端同一个事务里完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.version import LIFECYCLE, version_service

router = APIRouter(prefix="/api/version", tags=["采集配置版本"])

service = version_service

LIST_FIELDS = ["版本编号", "配置名称", "版本号", "变更说明", "变更内容", "回滚标识", "发布状态", "发布时间"]
STATUSES = list(LIFECYCLE.states)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按版本编号检索"),
    status: str | None = Query(default=None, description="草稿、已发布、已回滚、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按版本编号与状态过滤采集配置版本列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集配置版本清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "version", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条配置版本明细与它的审计留痕；不存在时给出可读的错误说明。"""
    detail = service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"配置版本 {entry_id} 不存在或已归档")
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条配置版本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="配置版本已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条配置版本执行发布版本、回滚版本、废弃版本。

    状态迁移与审计留痕在同一个事务边界里完成：动作失败时版本不变、不追加审计；
    带上 base_status 可以拦截并发覆盖——状态已被别的请求推进过时整体失败。
    """
    action = str(payload.values.get("action") or payload.action or "").strip()
    entry, event, message = service.run_action(
        entry_id,
        action,
        base_status=payload.values.get("base_status"),
        operator=payload.values.get("执行人"),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, audit=event)
