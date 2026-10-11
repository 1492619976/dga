"""审批门：写操作的【变更计划卡】生命周期 + 工具侧双层校验。

编排层：create_plan() 生成计划卡 → 用户确认 confirm_plan() → 子 Agent 带 plan_id 调工具。
工具层：assert_valid_plan() 校验（第二道防线，防止绕过编排层直调工具）。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta

from ..meta import store

PLAN_TTL_MINUTES = 5  # 确认后 5 分钟内有效


def create_plan(conn: sqlite3.Connection, actions: list[dict]) -> str:
    """actions: [{tool, object, change, impact, rollback}]，生成 pending 计划卡。"""
    plan_id = "p" + secrets.token_hex(3)
    conn.execute(
        "INSERT INTO approval_plan(id, actions_json, status, created_at) "
        "VALUES (?,?,'pending',?)", (plan_id, json.dumps(actions, ensure_ascii=False),
                                     datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return plan_id


def confirm_plan(conn: sqlite3.Connection, plan_id: str) -> bool:
    cur = conn.execute(
        "UPDATE approval_plan SET status='approved', decided_at=? WHERE id=? AND status='pending'",
        (datetime.now().isoformat(timespec="seconds"), plan_id))
    conn.commit()
    return cur.rowcount > 0


def cancel_plan(conn: sqlite3.Connection, plan_id: str) -> bool:
    cur = conn.execute(
        "UPDATE approval_plan SET status='cancelled', decided_at=? WHERE id=? AND status='pending'",
        (datetime.now().isoformat(timespec="seconds"), plan_id))
    conn.commit()
    return cur.rowcount > 0


def assert_valid_plan(conn: sqlite3.Connection, plan_id: str, tool_name: str) -> None:
    """工具写操作第一行调用：plan 必须存在、approved、未过期、且动作里含该工具。"""
    r = conn.execute("SELECT * FROM approval_plan WHERE id=?", (plan_id,)).fetchone()
    if r is None:
        raise PermissionError(f"plan not found: {plan_id}")
    if r["status"] != "approved":
        raise PermissionError(f"plan {plan_id} status={r['status']} (need approved)")
    decided = datetime.fromisoformat(r["decided_at"])
    if datetime.now() - decided > timedelta(minutes=PLAN_TTL_MINUTES):
        raise PermissionError(f"plan {plan_id} expired (TTL={PLAN_TTL_MINUTES}min)")
    actions = json.loads(r["actions_json"])
    if not any(a.get("tool") == tool_name for a in actions):
        raise PermissionError(f"plan {plan_id} does not cover tool {tool_name}")
