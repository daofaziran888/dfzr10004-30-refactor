"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
并发与事务语义也按数据库的方式提供：``locked`` 串行化读写，``transaction``
把跨模块的写入包进同一个边界，中途失败整体回滚，不留下半边数据。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    @contextmanager
    def locked(self) -> Iterator[None]:
        """串行化一段读写：并发的复合读（如详情+审计留痕）拿到一致的结果。"""
        with self._lock:
            yield

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """把涉及若干模块的写入包进同一个事务边界。

        进入时持有锁，整段校验与写入串行执行；任何一步失败，涉及的模块整体
        回滚到进入前的样子，版本状态与审计事件要么一起落库、要么都不落。
        """
        with self._lock:
            snapshots = {name: [dict(row) for row in self.rows(name)] for name in modules}
            try:
                yield
            except Exception:
                for name, rows in snapshots.items():
                    self._tables[name] = rows
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
