"""报送工具（5 个，v0.2 完整实现）：规范问答 / 映射三类检查 / 校验规则生成 / 预览 / 取数 SQL。

铁律：回答带出处（文档名+章节）；报送 SQL 永远只生成不执行；预览 PII 一律脱敏。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import duckdb

from ..config import BUSINESS_DB, CONFIGS_DIR, META_DB, SPEC_DOCS_DIR
from ..meta import store
from .sql_guard import check

REPORT_TABLES = {
    "RPT_CUST_INFO": {"spec": "客户信息报送规范-模拟", "src": "ods_customer_src",
                      "required": ["cust_no", "cust_name", "cert_type", "cert_no", "phone",
                                   "email", "addr", "open_date", "status"]},
    "RPT_ACCT_INFO": {"spec": "账户信息报送规范-模拟", "src": "dwd_account",
                      "required": ["acct_no", "cust_no", "balance", "currency", "status"]},
    "RPT_TRX_INFO": {"spec": "交易信息报送规范-模拟", "src": "dwd_trx",
                     "required": ["trx_id", "acct_no", "cust_no", "trx_date", "amt",
                                  "dr_cr", "channel", "status"]},
}


def check_mapping(report_table: str) -> dict[str, Any]:
    """映射三类检查：缺失映射 / 类型不匹配（有 CAST 标"经转换"）/ 码值不一致（distinct 真比对）。"""
    cfg = REPORT_TABLES.get(report_table)
    if not cfg:
        return {"error": f"未登记的报送表: {report_table}（已登记：{list(REPORT_TABLES)}）"}
    with store.connect(META_DB) as conn:
        mappings = store.list_report_mapping(conn, report_table)
        stds = {s["id"]: s for s in store.list_standards(conn)}

    by_field = {m["report_field"]: m for m in mappings}
    missing = [f for f in cfg["required"] if f not in by_field or not by_field[f]["src_field"]]

    type_issues, code_issues = [], []
    biz = duckdb.connect(str(BUSINESS_DB), read_only=True)
    try:
        meta_cols = _src_meta_cols(cfg["src"])
        for f in cfg["required"]:
            m = by_field.get(f)
            if not m or not m["src_field"]:
                continue
            col = meta_cols.get(m["src_field"])
            if col and col["compliance_id"]:
                std = stds.get(col["compliance_id"])
                if std and std.get("code_values"):
                    valid = {c["v"] for c in json.loads(std["code_values"])}
                    sql = check(f"SELECT DISTINCT {m['src_field']} FROM {cfg['src']} "
                                f"WHERE {m['src_field']} IS NOT NULL")
                    got = {str(r[0]) for r in biz.execute(sql).fetchall()}
                    bad = sorted(got - valid)
                    if bad:
                        code_issues.append({"field": f, "src_col": m["src_field"],
                                            "std": std["id"], "invalid": bad})
            transform = m.get("transform") or ""
            if f == "open_date" and "CAST" not in transform and meta_cols.get(m["src_field"], {}).get("col_type") == "VARCHAR":
                type_issues.append({"field": f, "src_col": m["src_field"],
                                    "issue": "源 VARCHAR → 报送 DATE，未配置 CAST 转换"})
    finally:
        biz.close()
    return {"report_table": report_table, "spec": cfg["spec"], "source": cfg["src"],
            "mapped": len(cfg["required"]) - len(missing), "required": len(cfg["required"]),
            "missing": missing, "type_issues": type_issues, "code_issues": code_issues}


def gen_check_rules(report_table: str) -> list[dict[str, str]]:
    """按规范生成报送校验规则四类：非空/格式/码值/勾稽。"""
    cfg = REPORT_TABLES[report_table]
    src = cfg["src"]
    rules = []
    for f in cfg["required"]:
        col = _map_col(report_table, f)
        if col:
            rules.append({"type": "非空", "rule": f"{f} 必填",
                          "sql": f"SELECT COUNT(*) FROM {src} WHERE {col} IS NULL OR {col}=''"})
    rules.append({"type": "格式", "rule": "手机号 11 位且 1 开头",
                  "sql": f"SELECT COUNT(*) FROM {src} WHERE MobilePhone IS NOT NULL "
                         f"AND NOT regexp_matches(MobilePhone, '^1[3-9][0-9]{{9}}$')"})
    rules.append({"type": "码值", "rule": "证件类型 ∈ {1,2,3,4,5}",
                  "sql": f"SELECT COUNT(*) FROM {src} WHERE CERTYPE NOT IN ('1','2','3','4','5')"})
    if report_table == "RPT_CUST_INFO":
        rules.append({"type": "勾稽", "rule": "客户表客户数 ≈ 账户表 distinct 客户数（容忍0.5%）",
                      "sql": "SELECT (SELECT COUNT(DISTINCT Cust_No) FROM ods_customer_src) AS a, "
                             "(SELECT COUNT(DISTINCT cust_no) FROM dwd_account) AS b, "
                             "ABS((SELECT COUNT(DISTINCT Cust_No) FROM ods_customer_src) - "
                             "(SELECT COUNT(DISTINCT cust_no) FROM dwd_account))*1.0 / "
                             "NULLIF((SELECT COUNT(DISTINCT Cust_No) FROM ods_customer_src),0) AS diff_rate"})
    return rules


def preview_report(report_table: str, limit: int = 100) -> dict[str, Any]:
    """报送宽表预览：只读执行 + 完整率 + 5 行脱敏样例。"""
    cfg = REPORT_TABLES[report_table]
    with store.connect(META_DB) as conn:
        mappings = store.list_report_mapping(conn, report_table)
    pairs = [(m["report_field"], m["src_field"], m.get("transform") or "")
             for m in mappings if m["src_field"]]
    if not pairs:
        return {"error": "无有效映射"}
    selects = []
    for rf, sf, tf in pairs:
        expr = f"CAST({sf} AS DATE)" if "DATE" in tf.upper() else sf
        selects.append(f"{expr} AS {rf}")
    src = cfg["src"]
    sql = check(f"SELECT {', '.join(selects)} FROM {src} LIMIT {limit}")
    biz = duckdb.connect(str(BUSINESS_DB), read_only=True)
    try:
        cur = biz.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        total = biz.execute(check(f"SELECT COUNT(*) FROM {src}")).fetchone()[0]
    finally:
        biz.close()

    required = set(cfg["required"])
    completeness = {}
    for i, c in enumerate(cols):
        nonnull = sum(1 for r in rows if r[i] not in (None, ""))
        completeness[c] = round(nonnull / max(len(rows), 1), 4)
    pii_like = {"cert_no", "phone", "email", "cust_name"}

    def mask(c, v):
        if c in pii_like and v not in (None, ""):
            return "***"
        return None if v is None else str(v)  # Decimal/日期统一转字符串可序列化
    sample = [{c: mask(c, r[i]) for i, c in enumerate(cols)} for r in rows[:5]]
    missing = sorted(required - set(cols))
    return {"report_table": report_table, "rows_previewed": len(rows), "source_rows": total,
            "completeness": completeness, "missing_fields": missing, "sample": sample}


def gen_report_sql(report_table: str, session: str = "web") -> dict[str, Any]:
    """生成报送取数 SQL（INSERT INTO ... SELECT），只生成不执行。"""
    cfg = REPORT_TABLES[report_table]
    with store.connect(META_DB) as conn:
        mappings = store.list_report_mapping(conn, report_table)
    pairs = [(m["report_field"], m["src_field"], m.get("transform") or "")
             for m in mappings if m["src_field"]]
    fields = ", ".join(p[0] for p in pairs)
    exprs = ", ".join((f"CAST({sf} AS DATE) AS {rf}" if "DATE" in tf.upper() else f"{sf} AS {rf}")
                      for rf, sf, tf in pairs)
    sql = (f"INSERT INTO {report_table} ({fields})\nSELECT {exprs}\nFROM {cfg['src']};")
    CONFIGS_DIR.mkdir(exist_ok=True)
    out = CONFIGS_DIR / f"report_{report_table}.sql"
    out.write_text(f"-- DGA 生成（{cfg['spec']}）· 人工审核后交调度执行\n{sql}\n", encoding="utf-8")
    with store.connect(META_DB) as conn:
        store.append_audit(conn, session, "report", "gen_report_sql", report_table, None, "ok")
    return {"report_table": report_table, "sql": sql, "file": str(out),
            "note": "已生成，未执行——执行权在用户（交调度）"}


# ---- 规范问答 ----

def ask_spec(question: str) -> dict[str, Any]:
    """规范文档检索（v0.2 关键词 BM25-lite；W13 换 Chroma+bge-m3）。无引用不输出。"""
    words = [w for w in re.split(r"\s|，|。|？|的|是|什么|哪些|要求", question) if len(w) >= 2]
    best, best_hits, best_lines, chapter = None, 0, [], ""
    for d in Path(SPEC_DOCS_DIR).glob("*.md"):
        text = d.read_text(encoding="utf-8")
        lines = text.splitlines()
        chap = ""
        hits, picked = 0, []
        for ln in lines:
            if re.match(r"## 第", ln):
                chap = ln.strip("# ").strip()
            for w in words:
                if w in ln:
                    hits += 1
                    if ln.strip() and ln not in picked and not ln.startswith("#") and not ln.startswith("|---"):
                        picked.append(ln.strip())
        if hits > best_hits:
            best, best_hits, best_lines, chapter = d, hits, picked[:4], chap
    if not best or best_hits < 2:
        return {"found": False,
                "message": "规范库中未找到相关条文，建议人工确认。已检索：《客户/账户/交易信息报送规范》3 篇（模拟）"}
    return {"found": True, "doc": best.stem, "chapter": chapter, "hits": best_hits,
            "clauses": best_lines,
            "source": f"📚 出处：《{best.stem}》{chapter}"}


# ---- 内部 ----

def _src_meta_cols(src_table: str) -> dict[str, dict]:
    with store.connect(META_DB) as conn:
        t = store.find_table(conn, 1, "main", src_table)
        if not t:
            return {}
        return {c["col_name"]: c for c in store.columns_of(conn, t["id"])}


def _map_col(report_table: str, field: str) -> str | None:
    with store.connect(META_DB) as conn:
        for m in store.list_report_mapping(conn, report_table):
            if m["report_field"] == field and m["src_field"]:
                return m["src_field"]
    return None
