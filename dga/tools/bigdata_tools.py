"""Apache 大数据组件工具组（demo 模式）——Flink / Kafka / HDFS / ZooKeeper / Flume。

设计：接口语义与真实组件一致；未配置真实端点时运行在本地 demo 模式
（Kafka=SQLite 消息表，HDFS=本地目录树，ZK=SQLite znode 表，Flink=作业注册表+计划预览）。
配置真实端点的方法见各工具 docstring（W14 接真实 client）。

安全：所有方法只做登记/预览/本地模拟，不触碰生产集群；produce/submit 过审计。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import CONFIGS_DIR, DEMO_DIR, META_DB
from ..meta.store import DDL, append_audit

HDFS_ROOT = DEMO_DIR / "hdfs_root"

_BIGDATA_DDL = """
CREATE TABLE IF NOT EXISTS kafka_msg(
  id INTEGER PRIMARY KEY, topic TEXT, ts TEXT, key TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS zk_node(
  path TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS flink_job(
  id INTEGER PRIMARY KEY, name TEXT, sql_text TEXT, plan_json TEXT,
  status TEXT, created_at TEXT);
"""

# demo 模式种子：模拟一个已在运行的数仓生态
_KAFKA_SEED = [
    ("dga.data.raw", "C000001", json.dumps({"table": "ods_trx_src", "op": "INSERT", "amt": 1200.50})),
    ("dga.data.raw", "C000002", json.dumps({"table": "ods_trx_src", "op": "INSERT", "amt": 88.00})),
    ("dga.data.raw", "A0000009", json.dumps({"table": "ods_acct_src", "op": "UPDATE", "balance": 53000})),
    ("dga.dq.alerts", "rule-105", json.dumps({"rule": "cert_type 码值超出标准", "table": "ods_customer_src", "level": "WARN"})),
    ("dga.dq.alerts", "rule-118", json.dumps({"rule": "phone 格式非法(11位校验)", "table": "ods_customer_src", "level": "ERROR"})),
]
_ZK_SEED = [
    ("/dga", "DGA 治理平台根节点"),
    ("/dga/datasources", "bank_retail"),
    ("/dga/jobs", "daily-etl, realtime-risk"),
    ("/brokers/ids", "0,1,2 (demo)"),
]
_HDFS_SEED = ["user/dga/data", "user/dga/report", "user/dga/tmp", "dwm"]


def init_bigdata_env() -> None:
    """建表+种子（build_demo 与首次使用都会调用，幂等）。"""
    conn = sqlite3.connect(META_DB)
    conn.executescript(_BIGDATA_DDL)
    if conn.execute("SELECT COUNT(*) FROM kafka_msg").fetchone()[0] == 0:
        for t, k, v in _KAFKA_SEED:
            conn.execute("INSERT INTO kafka_msg(topic,ts,key,value) VALUES (?,datetime('now'),?,?)", (t, k, v))
    if conn.execute("SELECT COUNT(*) FROM zk_node").fetchone()[0] == 0:
        conn.executemany("INSERT INTO zk_node(path,value,updated_at) VALUES (?,?,datetime('now'))", _ZK_SEED)
    conn.commit()
    conn.close()
    for d in _HDFS_SEED:
        (HDFS_ROOT / d).mkdir(parents=True, exist_ok=True)


def _c() -> sqlite3.Connection:
    conn = sqlite3.connect(META_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_BIGDATA_DDL)
    return conn


# ---------------- Kafka ----------------

def kafka_topics() -> list[dict[str, Any]]:
    """列出 topic 与积压消息数（demo：SQLite 模拟 broker）。"""
    with _c() as c:
        rows = c.execute("SELECT topic, COUNT(*) n, MAX(ts) latest FROM kafka_msg GROUP BY topic").fetchall()
    return [{"topic": r["topic"], "messages": r["n"], "latest": r["latest"]} for r in rows]


def kafka_produce(topic: str, key: str, value: str, session: str = "web") -> dict[str, Any]:
    """发消息到 topic。真实模式：confluent-kafka Producer, bootstrap.servers 走配置。"""
    with _c() as c:
        c.execute("INSERT INTO kafka_msg(topic,ts,key,value) VALUES (?,datetime('now'),?,?)",
                  (topic, key, value))
        c.commit()
        total = c.execute("SELECT COUNT(*) FROM kafka_msg WHERE topic=?", (topic,)).fetchone()[0]
        append_audit(c, session, "bigdata", "kafka_produce", f"{topic}:{key}", None, "ok")
    return {"topic": topic, "key": key, "produced": True, "topic_size": total}


def kafka_consume(topic: str, limit: int = 10) -> list[dict[str, Any]]:
    """消费 topic 最近 N 条（demo：不影响积压，等价 --from-beginning tail）。"""
    with _c() as c:
        rows = c.execute("SELECT ts, key, value FROM kafka_msg WHERE topic=? ORDER BY id DESC LIMIT ?",
                         (topic, limit)).fetchall()
    return [{"ts": r["ts"], "key": r["key"], "value": r["value"]} for r in rows][::-1]


# ---------------- HDFS (hadoop) ----------------

def hdfs_ls(path: str = "/") -> list[dict[str, Any]]:
    """列目录（demo：demo/hdfs_root 下的本地目录树）。"""
    p = _safe(path)
    if not p.exists():
        raise FileNotFoundError(f"HDFS 路径不存在: {path}")
    out = []
    for ch in sorted(p.iterdir()):
        out.append({"path": _norm(ch), "type": "dir" if ch.is_dir() else "file",
                    "size": ch.stat().st_size if ch.is_file() else "-"})
    return out


def hdfs_cat(path: str, max_bytes: int = 2048) -> str:
    """读文本文件内容（截断到 max_bytes）。"""
    p = _safe(path)
    if not p.is_file():
        raise FileNotFoundError(f"非文件: {path}")
    return p.read_text(encoding="utf-8")[:max_bytes]


def hdfs_put(path: str, content: str, session: str = "web") -> dict[str, Any]:
    """写文件到 HDFS（demo：写本地目录树）。报表/配置落盘走这里。"""
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    with _c() as c:
        append_audit(c, session, "bigdata", "hdfs_put", path, None, "ok")
    return {"path": _norm(p), "bytes": len(content.encode())}


# ---------------- ZooKeeper ----------------

def zk_get(path: str) -> dict[str, Any]:
    with _c() as c:
        r = c.execute("SELECT value, updated_at FROM zk_node WHERE path=?", (path,)).fetchone()
        children = c.execute("SELECT path FROM zk_node WHERE path LIKE ? AND path != ?",
                             (path + "/%", path)).fetchall()
    if not r:
        raise KeyError(f"znode 不存在: {path}")
    return {"path": path, "value": r["value"], "updated_at": r["updated_at"],
            "children": [x["path"] for x in children]}


def zk_set(path: str, value: str, session: str = "web") -> dict[str, Any]:
    """注册/更新 znode（治理元数据登记到 ZK 的模拟：如数据源上线登记）。"""
    with _c() as c:
        c.execute("INSERT INTO zk_node(path,value,updated_at) VALUES (?,?,datetime('now')) "
                  "ON CONFLICT(path) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                  (path, value))
        c.commit()
        append_audit(c, session, "bigdata", "zk_set", path, None, "ok")
    return {"path": path, "value": value}


# ---------------- Flink ----------------

def flink_sql_submit(sql: str, name: str | None = None, session: str = "web") -> dict[str, Any]:
    """提交 Flink SQL 作业（demo：sqlglot 解析/降级正则 → 生成作业 DAG 预览并登记）。

    真实模式：pyflink TableEnvironment.execute_sql()，endpoint 走配置（W14）。
    demo 保证：SQL 可解析性校验 + source/sink/算子提取 + 作业注册 RUNNING(demo)。
    """
    sinks = re.findall(r"INSERT\s+INTO\s+([A-Za-z_][\w.]*)", sql, re.I)
    sources = [t for t in re.findall(r"\bFROM\s+([A-Za-z_][\w.]*)", sql, re.I)]
    ops = []
    if re.search(r"\bTUMBLE\s*\(|GROUP BY\s+TUMBLE", sql, re.I):
        ops.append("TUMBLE 窗口聚合")
    if re.search(r"\bGROUP BY\b", sql, re.I):
        ops.append("分组聚合")
    if re.search(r"\bJOIN\b", sql, re.I):
        ops.append("双流 JOIN")
    if re.search(r"\bFILTER\b|\bWHERE\b", sql, re.I):
        ops.append("过滤")

    parse_via = None
    for dialect in ("duckdb", "presto", None):  # sqlglot 30.x 无 flink 方言，方言回退链
        try:
            import sqlglot
            sqlglot.parse_one(sql, read=dialect)
            parse_via = f"sqlglot-{dialect or 'generic'}"
            break
        except Exception:
            continue

    plan = {"sources": sorted(set(sources)), "sinks": sorted(set(sinks)) or ["(console)"],
            "operators": ops, "parse": parse_via or "regex-fallback"}
    dag = ["[Source] " + " → ".join(sorted(set(sources)) or ["?"])]
    for op in ops:
        dag.append(f"  ↓ {op}")
    dag.append("[Sink] " + ", ".join(sorted(set(sinks)) or ["console-print"]))

    with _c() as c:
        cur = c.execute("INSERT INTO flink_job(name,sql_text,plan_json,status,created_at) "
                        "VALUES (?,?,?,?,datetime('now'))",
                        (name or f"job_{datetime.now():%H%M%S}", sql,
                         json.dumps(plan, ensure_ascii=False), "RUNNING (demo)"))
        c.commit()
        jid = cur.lastrowid
        append_audit(c, session, "bigdata", "flink_sql_submit", f"job#{jid}", None, "ok")
    return {"job_id": jid, "status": "RUNNING (demo)", "plan": plan, "dag": "\n".join(dag)}


def flink_jobs() -> list[dict[str, Any]]:
    with _c() as c:
        rows = c.execute("SELECT id,name,status,created_at FROM flink_job ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


# ---------------- Flume ----------------

def flume_gen_config(agent: str = "a1", source: str = "syslog", sink: str = "hdfs",
                     session: str = "web") -> dict[str, Any]:
    """生成 Flume agent 配置（source→channel→sink 模板），存 configs/ 并落 HDFS。"""
    sink_conf = {
        "hdfs": "a1.sinks.k1.hdfs.path = hdfs://namenode/user/dga/data/%{topic}\na1.sinks.k1.hdfs.filePrefix = events-",
        "kafka": "a1.sinks.k1.kafka.topic = dga.data.raw\na1.sinks.k1.kafka.bootstrap.servers = localhost:9092",
        "logger": "a1.sinks.k1.type = logger",
    }[sink]
    conf = (f"a1.sources = r1\na1.sinks = k1\na1.channels = c1\n\n"
            f"a1.sources.r1.type = {source}\n"
            f"a1.channels.c1.type = memory\na1.channels.c1.capacity = 10000\n\n"
            f"a1.sinks.k1.type = { 'hdfs' if sink == 'hdfs' else 'org.apache.flume.sink.kafka.KafkaSink' if sink == 'kafka' else 'logger'}\n"
            f"{sink_conf}\n\n"
            f"a1.sources.r1.channels = c1\na1.sinks.k1.channel = c1\n")
    CONFIGS_DIR.mkdir(exist_ok=True)
    f = CONFIGS_DIR / f"flume-{agent}-{source}-{sink}.conf"
    f.write_text(conf, encoding="utf-8")
    with _c() as c:
        append_audit(c, session, "bigdata", "flume_gen_config", f.name, None, "ok")
    return {"config_file": str(f), "lines": conf.count("\n") + 1, "preview": conf[:400]}


# ---------------- 内部 ----------------

def _safe(path: str) -> Path:
    """HDFS 路径 → 本地 demo 目录（防穿越）。"""
    rel = path.strip().lstrip("/")
    p = (HDFS_ROOT / rel).resolve()
    if not str(p).startswith(str(HDFS_ROOT.resolve())):
        raise PermissionError(f"路径越界: {path}")
    return p


def _norm(p: Path) -> str:
    return "/" + str(p.relative_to(HDFS_ROOT)).replace("\\", "/")
