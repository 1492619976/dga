"""元数据库（SQLite）访问层：7 张核心表 + 种子 + DAO。

唯一事实源：治理 Agent 的一切回答以这里的元数据为准。
表结构见方案 v0.1 第 3.5 节；建库 DDL 与 demo/build_demo.py 共用本文件。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS datasource(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, url TEXT, type TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS meta_table(
  id INTEGER PRIMARY KEY, ds_id INT, db_name TEXT, table_name TEXT,
  row_cnt BIGINT, comment TEXT, pii_level TEXT, snapshot_at TEXT,
  UNIQUE(ds_id, db_name, table_name));
CREATE TABLE IF NOT EXISTS meta_column(
  id INTEGER PRIMARY KEY, table_id INT, col_name TEXT, col_type TEXT,
  comment TEXT, nullable INT DEFAULT 1, is_pii INT DEFAULT 0, compliance_id TEXT,
  FOREIGN KEY(table_id) REFERENCES meta_table(id));
CREATE TABLE IF NOT EXISTS gov_standard(
  id TEXT PRIMARY KEY, name TEXT, domain TEXT, def_text TEXT,
  std_type TEXT, code_values TEXT, naming_rule TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS metric_def(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, biz_text TEXT, calc_sql TEXT,
  owner TEXT, freq TEXT, grain TEXT, registered_at TEXT);
CREATE TABLE IF NOT EXISTS dq_rule(
  id INTEGER PRIMARY KEY, table_id INT, dim TEXT, expr_sql TEXT,
  threshold REAL, last_result TEXT, status TEXT DEFAULT 'active', created_at TEXT);
CREATE TABLE IF NOT EXISTS etl_sql(
  id INTEGER PRIMARY KEY, name TEXT, sql_text TEXT,
  target_table TEXT, schedule TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS report_mapping(
  id INTEGER PRIMARY KEY, report_table TEXT, report_field TEXT,
  src_table TEXT, src_field TEXT, transform TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT, session TEXT, agent TEXT, tool TEXT,
  args_digest TEXT, plan_id TEXT, result_status TEXT);
CREATE TABLE IF NOT EXISTS approval_plan(
  id TEXT PRIMARY KEY, actions_json TEXT,
  status TEXT DEFAULT 'pending', created_at TEXT, decided_at TEXT);
"""


def connect(meta_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(meta_db)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


# ---- DAO：薄封装，返回 dict 列表 ----

def rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def list_datasources(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn.execute("SELECT * FROM datasource ORDER BY id"))


def find_table(conn: sqlite3.Connection, ds_id: int, db: str, table: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM meta_table WHERE ds_id=? AND db_name=? AND table_name=?",
        (ds_id, db, table)).fetchone()
    return dict(r) if r else None


def columns_of(conn: sqlite3.Connection, table_id: int) -> list[dict]:
    return rows(conn.execute(
        "SELECT c.*, s.name AS std_name FROM meta_column c "
        "LEFT JOIN gov_standard s ON c.compliance_id = s.id "
        "WHERE c.table_id=? ORDER BY c.id", (table_id,)))


def list_standards(conn: sqlite3.Connection, domain: str | None = None) -> list[dict]:
    if domain:
        return rows(conn.execute(
            "SELECT * FROM gov_standard WHERE domain=? ORDER BY id", (domain,)))
    return rows(conn.execute("SELECT * FROM gov_standard ORDER BY id"))


def standard_code_values(std: dict) -> list[dict]:
    """码值 JSON → [{'v':..,'m':..}]；无码值返回 []。"""
    raw = std.get("code_values")
    return json.loads(raw) if raw else []


def get_metric(conn: sqlite3.Connection, name: str) -> dict | None:
    r = conn.execute("SELECT * FROM metric_def WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def list_metrics(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn.execute("SELECT id,name,biz_text,grain,freq,owner FROM metric_def ORDER BY id"))


def list_etl_sql(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn.execute("SELECT * FROM etl_sql ORDER BY id"))


def list_report_mapping(conn: sqlite3.Connection, report_table: str) -> list[dict]:
    return rows(conn.execute(
        "SELECT * FROM report_mapping WHERE report_table=? ORDER BY report_field",
        (report_table,)))


# ---- 写操作（工具层调用前必须已过 gate.assert_valid_plan） ----

def bind_compliance(conn: sqlite3.Connection, table_id: int, col_name: str,
                    standard_id: str) -> int:
    cur = conn.execute(
        "UPDATE meta_column SET compliance_id=? WHERE table_id=? AND col_name=?",
        (standard_id, table_id, col_name))
    if cur.rowcount == 0:
        raise KeyError(f"column not found: {col_name} (table_id={table_id})")
    conn.commit()
    return cur.rowcount


def update_column_comment(conn: sqlite3.Connection, table_id: int, col_name: str,
                          comment: str) -> int:
    cur = conn.execute(
        "UPDATE meta_column SET comment=? WHERE table_id=? AND col_name=?",
        (comment, table_id, col_name))
    conn.commit()
    return cur.rowcount


def mark_pii(conn: sqlite3.Connection, table_id: int, col_name: str, is_pii: bool) -> int:
    cur = conn.execute(
        "UPDATE meta_column SET is_pii=? WHERE table_id=? AND col_name=?",
        (1 if is_pii else 0, table_id, col_name))
    conn.commit()
    return cur.rowcount


def save_dq_rule(conn: sqlite3.Connection, table_id: int, dim: str, expr_sql: str,
                 threshold: float = 1.0) -> int:
    cur = conn.execute(
        "INSERT INTO dq_rule(table_id, dim, expr_sql, threshold, created_at) "
        "VALUES (?,?,?,?,datetime('now'))", (table_id, dim, expr_sql, threshold))
    conn.commit()
    return cur.lastrowid


def append_audit(conn: sqlite3.Connection, session: str, agent: str, tool: str,
                 args_digest: str, plan_id: str | None, status: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, session, agent, tool, args_digest, plan_id, result_status) "
        "VALUES (datetime('now'),?,?,?,?,?,?)",
        (session, agent, tool, args_digest, plan_id, status))
    conn.commit()
