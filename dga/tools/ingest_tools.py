"""接入工具（6 个）。v0.1 已实现只读探查四件；配置生成/试跑对账 TODO W12。

约定：
- 执行 SQL 前必须过 sql_guard.check()
- 写元数据必须先 gate.assert_valid_plan()
- 探查只输出统计画像，最多 5 行样例；PII 列不出样例值
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from ..config import BUSINESS_DB
from .sql_guard import check


def _conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(BUSINESS_DB), read_only=True)


def list_databases() -> list[dict[str, Any]]:
    """列出业务库中的 schema。"""
    with _conn() as c:
        rows = c.execute("SELECT schema_name FROM information_schema.schemata").fetchall()
    return [{"database": r[0]} for r in rows]


def list_tables(db: str = "main") -> list[dict[str, Any]]:
    """列出某 schema 下的表（名称/类型/行数）。"""
    sql = check(f"SELECT table_name, table_type FROM information_schema.tables "
                f"WHERE table_schema = '{db}' ORDER BY table_name")
    with _conn() as c:
        rows = c.execute(sql).fetchall()
        out = []
        for name, ttype in rows:
            cnt = c.execute(check(f"SELECT COUNT(*) FROM {db}.{name}")).fetchone()[0]
            out.append({"table": name, "type": ttype, "rows": cnt})
    return out


def get_schema(table: str, db: str = "main") -> dict[str, Any]:
    """表结构：字段名/类型/是否可空（duckdb 无注释列，注释在元数据库）。"""
    sql = check(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        f"WHERE table_schema='{db}' AND table_name='{table}' ORDER BY ordinal_position")
    with _conn() as c:
        cols = [{"name": n, "type": t, "nullable": (nu == "YES")}
                for n, t, nu in c.execute(sql).fetchall()]
    return {"table": f"{db}.{table}", "columns": cols}


_PII_PATTERNS = {  # 命中任一正则即标记（样例值不输出，仅统计）
    "cert_no": r"^\d{17}[\dX]$|^[P]\d{7}$|^9\d{16}[0-9X]$",
    "phone": r"^1[3-9]\d{9}$",
    "email": r"^[^@\s]+@[^@\s]+\.[a-z]+$",
}


def profile_table(table: str, db: str = "main", sample_rows: int = 5) -> dict[str, Any]:
    """表画像：行数 + 每列空值率/distinct/min/max/Top5。只读，过 sql_guard。"""
    schema = get_schema(table, db)
    fq = f"{db}.{table}"
    with _conn() as c:
        total = c.execute(check(f"SELECT COUNT(*) FROM {fq}")).fetchone()[0]
        col_stats = []
        for col in schema["columns"]:
            n = col["name"]
            if col["type"].startswith("DECIMAL") or col["type"] in ("INTEGER", "BIGINT", "DOUBLE"):
                stat = c.execute(check(
                    f"SELECT COUNT(*) FILTER (WHERE {n} IS NULL), COUNT(DISTINCT {n}), "
                    f"MIN({n}), MAX({n}) FROM {fq}")).fetchone()
                col_stats.append({"col": n, "type": col["type"], "null_rate": round(stat[0] / max(total, 1), 4),
                                  "distinct": stat[1], "min": str(stat[2]), "max": str(stat[3])})
            else:
                top = c.execute(check(
                    f"SELECT {n}, COUNT(*) FROM {fq} WHERE {n} IS NOT NULL "
                    f"GROUP BY {n} ORDER BY COUNT(*) DESC LIMIT 5")).fetchall()
                distinct = c.execute(check(f"SELECT COUNT(DISTINCT {n}) FROM {fq}")).fetchone()[0]
                nulls = c.execute(check(f"SELECT COUNT(*) FROM {fq} WHERE {n} IS NULL")).fetchone()[0]
                hits = sum(c.execute(check(
                    f"SELECT COUNT(*) FROM {fq} WHERE regexp_matches(CAST({n} AS VARCHAR), '{p}')"
                )).fetchone()[0] for p in _PII_PATTERNS.values())
                suspicious = hits > total * 0.5  # 多格式列（如三种证件号）按全模式合计判
                col_stats.append({"col": n, "type": col["type"], "null_rate": round(nulls / max(total, 1), 4),
                                  "distinct": distinct,
                                  "top5": [str(t[0]) for t in top], "suspected_pii": suspicious})
    return {"table": fq, "rows": total, "columns": col_stats}


# ---- TODO W12 ----
def connect_datasource(url: str, name: str):  # pragma: no cover
    """登记数据源：试连 + 三类失败诊断（网络/账号/驱动）+ 写 datasource 表（过审批）。"""
    raise NotImplementedError


def snapshot_metadata(table: str, db: str = "main"):  # pragma: no cover
    """schema+画像写入元数据库（写操作，过审批门）。"""
    raise NotImplementedError


def gen_sync_config(source_table: str, mode: str, increment_field: str | None = None,
                    target_table: str | None = None):  # pragma: no cover
    """生成 DataX JSON（full/increment）或 Flink SQL CDC（stream）到 ./configs/。"""
    raise NotImplementedError


def dry_run_check(config_path: Path):  # pragma: no cover
    """limit 100 双端对账（条数 + 3 字段值一致）。唯一允许 ATTACH 的工具。"""
    raise NotImplementedError
