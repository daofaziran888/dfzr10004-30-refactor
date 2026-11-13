"""共用状态机：所有业务模块的状态迁移只认这一份规则引擎。

每个模块用一份 ``Lifecycle`` 声明自己的状态序列与迁移规则，引擎统一负责：

- 旧动作名称兼容：别名映射只在声明处写一次，所有调用方归一后共用同一结果；
- 前置校验：动作只在声明的源状态下可执行，失败时给出可读原因、不产生任何写入；
- 应用迁移：状态、待处理标记、异常标记的写法全系统只有这一份；
- 可执行动作：列表、单条详情、前端按钮都读同一个 ``available_actions`` 结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Transition:
    """一条状态迁移规则：``action`` 把记录从 ``source`` 推进到 ``target``。

    ``negative`` 标记回退、停用这类有负面含义的动作，迁移后记录会带上异常标记。
    """

    action: str
    source: str
    target: str
    negative: bool = False


class Lifecycle:
    """一个模块的生命周期：状态序列 + 迁移规则 + 旧动作名映射，只声明一份。"""

    def __init__(
        self,
        *,
        label: str,
        states: list[str],
        transitions: list[Transition],
        aliases: dict[str, str] | None = None,
    ) -> None:
        if not states:
            raise ValueError("状态序列不能为空")
        self.label = label
        self.states = list(states)
        self.terminal = self.states[-1]
        self._aliases = dict(aliases or {})
        self._transitions: dict[str, Transition] = {}
        for transition in transitions:
            self._check_transition(transition)
            if transition.action in self._transitions:
                raise ValueError(f"动作「{transition.action}」的迁移规则重复声明")
            self._transitions[transition.action] = transition
        for alias, canonical in self._aliases.items():
            if canonical not in self._transitions:
                raise ValueError(f"旧动作名「{alias}」指向的「{canonical}」没有对应迁移规则")

    def _check_transition(self, transition: Transition) -> None:
        for status in (transition.source, transition.target):
            if status not in self.states:
                raise ValueError(f"状态「{status}」不在允许的状态序列里")
        if self.states.index(transition.target) < self.states.index(transition.source):
            raise ValueError(f"动作「{transition.action}」不允许把状态从「{transition.source}」倒退回「{transition.target}」")

    def canonical_action(self, action: str) -> str | None:
        """把调用方给出的动作名归一成声明过的动作；旧名称只在这里解释一次。"""
        name = str(action or "").strip()
        if name in self._transitions:
            return name
        mapped = self._aliases.get(name)
        if mapped and mapped in self._transitions:
            return mapped
        return None

    def available_actions(self, status: str) -> list[str]:
        """当前状态下可执行的动作清单：列表、详情、前端按钮共用这一份结果。"""
        return [t.action for t in self._transitions.values() if t.source == status]

    def evaluate(self, current_status: str, action: str) -> tuple[Transition | None, str | None]:
        """校验动作在当前状态下能否执行；不可执行时返回可读原因，不产生任何写入。"""
        canonical = self.canonical_action(action)
        if canonical is None:
            return None, f"动作「{action}」不属于{self.label}可执行范围"
        transition = self._transitions[canonical]
        if current_status != transition.source:
            return None, f"当前状态「{current_status}」不能执行{canonical}，需先处于「{transition.source}」"
        return transition, None

    def apply(self, entry: dict[str, Any], transition: Transition) -> None:
        """应用一次已校验的迁移：状态推进、待处理与异常标记的写法只有这一份。"""
        entry["status"] = transition.target
        entry["pending"] = transition.target != self.terminal
        entry["abnormal"] = transition.negative
