"""FastAPI 入口：/chat SSE 对话（router 规则编排）+ /api/sidebar + 静态前端。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import META_DB
from .graph import router
from .meta import store
from .tools import bigdata_tools as bd
from .tools import ingest_tools as ing

app = FastAPI(title="DGA · Data Governance Agent", version="0.1.0")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ChatIn(BaseModel):
    session: str = "web"
    message: str


@app.post("/chat")
async def chat(inp: ChatIn) -> StreamingResponse:
    async def gen():
        yield _sse("tool_call", {"tool": "supervisor.classify", "status": "running"})
        await asyncio.sleep(0.05)
        intent = router.classify(inp.message)
        yield _sse("tool_call", {"tool": f"route→{intent}", "status": "running"})
        try:
            result = router.handle(intent, inp.message, inp.session)
        except Exception as e:  # noqa: BLE001 —— 对话不能因单工具失败而断流
            result = {"reply": f"❌ 工具执行失败：{e}"}
        yield _sse("tool_call", {"tool": f"route→{intent}", "status": "done"})

        for i in range(0, len(result["reply"]), 24):  # 打字机分块
            yield _sse("token", {"text": result["reply"][i:i + 24]})
            await asyncio.sleep(0.012)
        if result.get("plan"):
            yield _sse("plan_card", result["plan"])
        yield _sse("receipt", {"done": True})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/sidebar")
async def sidebar():
    with store.connect(META_DB) as conn:
        audit_n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        std_n = conn.execute("SELECT COUNT(*) FROM gov_standard").fetchone()[0]
        metric_n = conn.execute("SELECT COUNT(*) FROM metric_def").fetchone()[0]
    return {
        "datasource": "bank_retail (duckdb)",
        "tables": ing.list_tables(),
        "flink_jobs": bd.flink_jobs()[:5],
        "kafka_topics": bd.kafka_topics(),
        "stats": {"standards": std_n, "metrics": metric_n, "audit": audit_n},
    }


@app.get("/audit")
async def audit(limit: int = 50):
    with store.connect(META_DB) as conn:
        return store.rows(conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
