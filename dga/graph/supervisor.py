"""入口 Supervisor：意图识别 → 槽位检查（澄清门）→ 路由子 Agent → 审批门。

W12 D1 实现计划（TODO）：
  1. DGAState(TypedDict)：session/history/slots{ds,table,metric}/pending_plan/recent_objects
  2. classify_intent 节点：prompts/supervisor.md 的路由表 few-shot（10 例）
  3. check_slots 节点：缺槽 → interrupt 反问（带候选项）
  4. route 条件边 → ingest / govern / report / help
  5. 写操作意图 → gate.create_plan() 出计划卡 → interrupt 等确认
  6. 回执节点统一按 supervisor.md 的回执格式渲染
工具结果不进长记忆：旁路存 SQLite/审计，仅回传摘要（学 ODR 压缩回传）。
"""
from __future__ import annotations

from typing import Any, TypedDict


class DGAState(TypedDict, total=False):
    session: str
    history: list[dict[str, str]]
    intent: str                 # ingest | govern | report | help
    slots: dict[str, Any]       # ds / table / metric / scope / range
    pending_plan: str | None    # gate plan_id
    recent_objects: list[str]   # 粘性上下文：最近 5 个操作对象
    final_answer: str


def build_graph():  # pragma: no cover - W12 D1
    """TODO: StateGraph(DGAState) 组装，langgraph dev 可视化调试。"""
    raise NotImplementedError("supervisor graph lands in W12 D1 (see module docstring)")
