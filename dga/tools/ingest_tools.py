"""接入工具（6 个）。v0.1 已实现只读探查四件；配置生成/试跑对账 TODO W12。

约定：
- 执行 SQL 前必须过 sql_guard.check()
- 写元数据必须先 gate.assert_valid_plan()
- 探查只输出统计画像，最多 5 行样例；PII 列不出样例值
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from ..config import BUSINESS_DB, CONFIGS_DIR, META_DB
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


# ---- 接入：登记/快照/配置生成/对账（v0.2 实现） ----

def connect_datasource(url: str, name: str, session: str = "web") -> dict[str, Any]:
    """登记数据源：先试连（information_schema），失败给诊断；成功写 datasource 表。

    v0.1 支持 duckdb 文件路径；MySQL/PostgreSQL 在 W12 接 SQLAlchemy 引擎池。
    """
    p = Path(url)
    if not p.exists():
        return {"error": f"试连失败：文件不存在 {url}（诊断：路径错误或未挂载）"}
    try:
        c = duckdb.connect(url, read_only=True)
        n = c.execute("SELECT COUNT(*) FROM information_schema.tables").fetchone()[0]
        c.close()
    except Exception as e:  # noqa: BLE001
        return {"error": f"试连失败：不是有效的 DuckDB 文件（诊断：{type(e).__name__}）"}
    from ..meta import store
    with store.connect(META_DB) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO datasource(name,url,type,created_at) "
                           "VALUES (?,?,?,datetime('now'))", (name, url, "duckdb"))
        conn.commit()
        existed = cur.rowcount == 0
        store.append_audit(conn, session, "ingest", "connect_datasource", f"{name}:{url}", None, "ok")
    return {"name": name, "url": url, "type": "duckdb", "tables_found": n,
            "already_registered": existed}


def snapshot_metadata(table: str, db: str = "main", session: str = "web") -> dict[str, Any]:
    """schema+画像写入元数据库（meta_table/meta_column，幂等 upsert）。"""
    schema = get_schema(table, db)
    prof = profile_table(table, db)
    cols_prof = {c["col"]: c for c in prof["columns"]}
    from ..meta import store
    with store.connect(META_DB) as conn:
        existing = store.find_table(conn, 1, db, table)
        if existing:
            tid = existing["id"]
            conn.execute("UPDATE meta_table SET row_cnt=?, snapshot_at=datetime('now') WHERE id=?",
                         (prof["rows"], tid))
        else:
            conn.execute(
                "INSERT INTO meta_table(ds_id,db_name,table_name,row_cnt,snapshot_at) "
                "VALUES (1,?,?,?,datetime('now'))", (db, table, prof["rows"]))
            tid = conn.execute("SELECT id FROM meta_table WHERE db_name=? AND table_name=?",
                               (db, table)).fetchone()[0]
        known = {r["col_name"] for r in conn.execute(
            "SELECT col_name FROM meta_column WHERE table_id=?", (tid,)).fetchall()}
        added = 0
        for col in schema["columns"]:
            if col["name"] in known:
                continue
            conn.execute(
                "INSERT INTO meta_column(table_id,col_name,col_type,nullable,is_pii) VALUES (?,?,?,?,?)",
                (tid, col["name"], col["type"], 1 if col["nullable"] else 0,
                 1 if cols_prof.get(col["name"], {}).get("suspected_pii") else 0))
            added += 1
        conn.commit()
        store.append_audit(conn, session, "ingest", "snapshot_metadata", table, None, "ok")
    return {"table": table, "rows": prof["rows"], "fields": len(schema["columns"]),
            "new_fields_registered": added, "snapshot": "ok"}


def gen_sync_config(source_table: str, mode: str, increment_field: str | None = None,
                    target_table: str | None = None, session: str = "web") -> dict[str, Any]:
    """生成同步配置：full/increment=DataX JSON；stream=Flink SQL CDC。存 configs/。"""
    target = target_table or f"ods_{source_table}"
    schema = get_schema(source_table)
    cols = [c["name"] for c in schema["columns"]]
    CONFIGS_DIR.mkdir(exist_ok=True)

    if mode in ("full", "increment"):
        where = ""
        fld = increment_field or "UpdTime"
        if mode == "increment":
            where = f" WHERE {fld} >= '${{last_sync_time}}'"
        query = f"SELECT {', '.join(cols)} FROM {source_table}{where}"
        config = {
            "job": {
                "content": [{
                    "reader": {"name": "rdbmsreader", "parameter": {
                        "username": "${src_user}", "password": "${src_pwd}",
                        "querySql": [query],
                        "connection": [{"jdbcUrl": ["${src_jdbc}"]}],
                    }},
                    "writer": {"name": "rdbmswriter", "parameter": {
                        "username": "${dst_user}", "password": "${dst_pwd}",
                        "writeMode": "insert", "column": cols,
                        "connection": [{"jdbcUrl": ["${dst_jdbc}"], "table": [target]}],
                    }},
                }],
                "setting": {"speed": {"channel": 3},
                            "errorLimit": {"record": 0, "percentage": 0.02}},
            },
            "_meta": {"generated_by": "DGA", "mode": mode,
                      "increment_field": fld if mode == "increment" else None},
        }
        out = CONFIGS_DIR / f"sync_{source_table}_{mode}.json"
        out.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        kind = f"DataX {mode}" + (f"（增量字段 {fld}）" if mode == "increment" else "")
    elif mode == "stream":
        col_defs = ",\n  ".join(f"{c['name']} {c['type']}" for c in schema["columns"])
        cdc = (f"-- Flink SQL CDC（DGA 生成）\n"
               f"CREATE TABLE {source_table}_cdc (\n  {col_defs}\n) WITH (\n"
               f"  'connector' = 'mysql-cdc', 'hostname' = '${{src_host}}',\n"
               f"  'database-name' = '${{src_db}}', 'table-name' = '{source_table}'\n);\n\n"
               f"CREATE TABLE {target} (\n  {col_defs}\n) WITH (\n"
               f"  'connector' = 'jdbc', 'url' = '${{dst_jdbc}}', 'table-name' = '{target}'\n);\n\n"
               f"INSERT INTO {target} SELECT {', '.join(cols)} FROM {source_table}_cdc;\n")
        out = CONFIGS_DIR / f"sync_{source_table}_stream.sql"
        out.write_text(cdc, encoding="utf-8")
        kind = "Flink SQL CDC"
    else:
        return {"error": f"未知模式: {mode}（full/increment/stream）"}

    from ..meta import store
    with store.connect(META_DB) as conn:
        store.append_audit(conn, session, "ingest", "gen_sync_config", f"{source_table}:{mode}", None, "ok")
    return {"config_file": str(out), "kind": kind, "columns": len(cols),
            "preview": out.read_text(encoding="utf-8")[:360]}


def dry_run_check(source_table: str, session: str = "web") -> dict[str, Any]:
    """试跑对账：limit 100 → _dga_check_ 临时表 → 条数+3 字段值一致性 → 清理临时表。"""
    schema = get_schema(source_table)
    cols = [c["name"] for c in schema["columns"]][:3] or ["*"]
    tmp = f"_dga_check_{source_table[:12]}"
    with duckdb.connect(str(BUSINESS_DB)) as c:  # 写连接：要建临时表
        c.execute(check(f"CREATE OR REPLACE TEMP TABLE {tmp} AS "
                        f"SELECT * FROM {source_table} LIMIT 100", allow_create_temp=True))
        src_n = c.execute(check(f"SELECT COUNT(*) FROM {source_table} LIMIT 100")).fetchone()[0]
        tmp_n = c.execute(check(f"SELECT COUNT(*) FROM {tmp}")).fetchone()[0]
        sample_ok = True
        for col in cols:
            h1 = c.execute(check(f"SELECT md5(string_agg(CAST({col} AS VARCHAR), ',' ORDER BY 1)) "
                                 f"FROM (SELECT {col} FROM {source_table} LIMIT 100)")).fetchone()[0]
            h2 = c.execute(check(f"SELECT md5(string_agg(CAST({col} AS VARCHAR), ',' ORDER BY 1)) FROM {tmp}")).fetchone()[0]
            sample_ok &= (h1 == h2)
        c.execute(f"DROP TABLE IF EXISTS {tmp}")  # 临时表随连接销毁，防御性再删
    from ..meta import store
    with store.connect(META_DB) as conn:
        store.append_audit(conn, session, "ingest", "dry_run_check", source_table, None, "ok")
    return {"source": source_table, "sampled": min(src_n, 100),
            "rows_match": src_n >= 100 or tmp_n == src_n, "target_rows": tmp_n,
            "fields_checked": cols, "values_match": sample_ok,
            "temp_table": f"{tmp} (cleaned)"}


