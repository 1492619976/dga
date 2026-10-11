"""FastAPI 入口：/chat SSE 流式对话 + /audit 审计查询 + /plan/{id}/confirm 审批。

W12 D1 接 LangGraph；当前骨架可启动并返回占位事件（前端联调通路先行）。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import META_DB
from .meta import store

app = FastAPI(title="DGA · Data Governance Agent", version="0.1.0")


class ChatIn(BaseModel):
    session: str = "default"
    message: str


@app.post("/chat")
async def chat(inp: ChatIn) -> StreamingResponse:
    """SSE 事件：token / tool_call / plan_card / receipt。W12 接 graph.supervisor。"""
    async def gen():
        yield sse("token", {"text": "DGA v0.1 骨架已就绪。对话编排层在 W12 D1 接入 LangGraph。\n"})
        yield sse("token", {"text": f"收到：{inp.message}\n目前可先用 demo 数据与工具面：python -c "
                                    f"'from dga.tools.ingest_tools import list_tables; print(list_tables())'"})
        yield sse("receipt", {"done": True})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/audit")
async def audit(limit: int = 50):
    with store.connect(META_DB) as conn:
        return store.rows(conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))


@app.post("/plan/{plan_id}/confirm")
async def confirm(plan_id: str):
    from .graph import gate
    with store.connect(META_DB) as conn:
        ok = gate.confirm_plan(conn, plan_id)
    return {"plan_id": plan_id, "confirmed": ok}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
