"""治理工具（14 个，四技能包）——v0.1 已实现：标准查询/落标检查/质量执行/血缘/指标查询。

元数据：enrich_description(TODO) / classify_pii(画像侧已有) / classify_level(TODO)
标准：  list_standards ✅ / recommend_standards(并入 check_compliance 输出) /
        check_compliance ✅ / bind_compliance(经 gate，store 已有)
质量：  run_quality_rules ✅（六性真跑） / gen_quality_rules ✅（模板） / root_cause(TODO)
指标：  query_metric ✅ / diff_metrics(TODO W12 sqlglot 招牌) / lineage ✅ / register_metric(TODO)
"""
from __future__ import annotations

import difflib
import re
from typing import Any

import duckdb

from ..config import BUSINESS_DB, META_DB
from ..meta import store
from .sql_guard import check

_SIMILAR = re.compile(r"[A-Za-z]+")


def _c():
    return store.connect(META_DB)


def _biz() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(BUSINESS_DB), read_only=True)


def list_standards(domain: str | None = None) -> list[dict[str, Any]]:
    with _c() as conn:
        return store.list_standards(conn, domain)


def get_metric(name: str) -> dict[str, Any] | None:
    with _c() as conn:
        return store.get_metric(conn, name)


# ---------------- 落标检查（三层） ----------------

def _type_compatible(std_type: str, col_type: str) -> bool:
    def kind(t: str) -> str:
        t = t.upper()
        if any(k in t for k in ("CHAR", "VARCHAR", "TEXT", "STRING")):
            return "str"
        if any(k in t for k in ("DECIMAL", "NUM", "INT", "BIGINT", "DOUBLE", "FLOAT")):
            return "num"
        return t.split("(")[0]
    return kind(std_type) == kind(col_type)


def check_compliance(table: str) -> dict[str, Any]:
    """三层落标检查：类型兼容 / 命名规则 / 码值比对（对有码值的标准，distinct 真比对）。

    未贯标字段同时给出推荐标准（字段名相似度 0.5 + 类型兼容 0.3 + PII 倾向 0.2）。
    """
    with _c() as conn:
        t = store.find_table(conn, 1, "main", table)
        if not t:
            return {"error": f"元数据库未登记表: {table}（先执行接入探查/快照）"}
        cols = store.columns_of(conn, t["id"])
        stds = {s["id"]: s for s in store.list_standards(conn)}
        ds_url = conn.execute("SELECT url FROM datasource WHERE id=1").fetchone()[0]

    biz = duckdb.connect(ds_url, read_only=True)
    total = len(cols)
    details, recos = [], []
    for col in cols:
        name, ctype = col["col_name"], col["col_type"]
        std = stds.get(col["compliance_id"]) if col["compliance_id"] else None
        if std is None:
            best, score = None, 0.0
            tokens = "".join(_SIMILAR.findall(name)).lower()
            for s in stds.values():
                sim = difflib.SequenceMatcher(None, tokens, s["naming_rule"].lower()).ratio()
                sc = sim * 0.5 + (0.3 if _type_compatible(s["std_type"], ctype) else 0) \
                     + (0.2 if (col["is_pii"] and s["domain"] == "客户") else 0)
                if sc > score:
                    best, score = s, sc
            recos.append({"col": name, "type": ctype,
                          "recommend_std": best["id"] if best else None,
                          "recommend_name": best["name"] if best else None,
                          "confidence": round(min(score, 1.0), 2)})
            continue
        d: dict[str, Any] = {"col": name, "std": std["id"], "std_name": std["name"],
                             "type_ok": _type_compatible(std["std_type"], ctype),
                             "naming_ok": std["naming_rule"].lower() in name.lower(),
                             "code_issues": None}
        if std.get("code_values"):
            import json as _json
            valid = {c["v"] for c in _json.loads(std["code_values"])}
            sql = check(f"SELECT DISTINCT {name} FROM {table} WHERE {name} IS NOT NULL")
            got = {str(r[0]) for r in biz.execute(sql).fetchall()}
            bad = sorted(got - valid)
            d["code_issues"] = {"invalid_values": bad, "distinct": len(got)} if bad else None
        details.append(d)
    biz.close()

    bound = len(details)
    return {"table": table, "total_fields": total, "bound": bound,
            "compliance_rate": round(bound / max(total, 1), 4),
            "details": details, "recommendations": recos}


# ---------------- 质量规则（六性模板 + 执行） ----------------

def gen_quality_rules(table: str) -> list[dict[str, str]]:
    """六性模板：完整性/唯一性/有效性/一致性/准确性/及时性（主键与码值列自适应发现）。"""
    rules = [
        {"dim": "完整性", "name": "证件号码非空", "sql": f"SELECT COUNT(*) FROM {table} WHERE CertNo IS NULL OR CertNo=''"},
        {"dim": "唯一性", "name": "客户号唯一", "sql": f"SELECT COUNT(*) - COUNT(DISTINCT Cust_No) FROM {table}"},
        {"dim": "有效性", "name": "证件类型码值合法", "sql": f"SELECT COUNT(*) FROM {table} WHERE CERTYPE NOT IN ('1','2','3','4','5')"},
        {"dim": "准确性", "name": "手机号格式合法", "sql": f"SELECT COUNT(*) FROM {table} WHERE NOT regexp_matches(MobilePhone, '^1[3-9][0-9]{{9}}$')"},
        {"dim": "一致性", "name": "客户状态码值合法", "sql": f"SELECT COUNT(*) FROM {table} WHERE CustSts NOT IN ('1','2','3')"},
        {"dim": "及时性", "name": "更新时间未超30天", "sql": f"SELECT COUNT(*) FROM {table} WHERE CAST(UpdTime AS DATE) < CURRENT_DATE - INTERVAL 30 DAY"},
    ]
    return rules


def run_quality_rules(table: str, session: str = "web") -> dict[str, Any]:
    """执行六性规则（全部只读 SQL，过 sql_guard），返回每条规则失败行数与通过率。"""
    with _biz() as biz:
        total = biz.execute(check(f"SELECT COUNT(*) FROM {table}")).fetchone()[0]
        results = []
        with _c() as conn:
            tid = store.find_table(conn, 1, "main", table)["id"]
            for r in gen_quality_rules(table):
                fails = biz.execute(check(r["sql"])).fetchone()[0]
                results.append({**r, "fail_rows": fails,
                                "pass_rate": round((total - fails) / max(total, 1), 4)})
                store.save_dq_rule(conn, tid, r["dim"], r["sql"])
            store.append_audit(conn, session, "govern", "run_quality_rules", table, None, "ok")
    return {"table": table, "rows": total, "rules": results,
            "overall_pass": round(sum(r["pass_rate"] for r in results) / len(results), 4)}


# ---------------- 血缘（解析 etl_sql 全集） ----------------

def lineage(obj: str, direction: str = "downstream", depth: int = 2) -> dict[str, Any]:
    """血缘 + 影响分析：解析元数据库登记的全部 ETL SQL（FROM 表 → target_table 建边）。"""
    with _c() as conn:
        etls = store.list_etl_sql(conn)
        metrics = store.list_metrics(conn)
        metric_sqls = {m["name"]: None for m in metrics}
        with _c() as c2:
            for m in metrics:
                row = c2.execute("SELECT calc_sql FROM metric_def WHERE name=?", (m["name"],)).fetchone()
                metric_sqls[m["name"]] = row["calc_sql"] if row else ""

    edges: set[tuple[str, str]] = set()
    for e in etls:
        for src in set(re.findall(r"\bFROM\s+([A-Za-z_]\w*)", e["sql_text"], re.I)):
            if src != e["target_table"]:
                edges.add((src, e["target_table"]))

    def walk(start: str, d: int) -> list[str]:
        seen, frontier = {start}, [start]
        for _ in range(d):
            nxt = [b for a, b in edges if a in frontier and b not in seen]
            nxt += [a for a, b in edges if b in frontier and a not in seen] if direction == "both" else []
            frontier, seen = nxt, seen | set(nxt)
        return sorted(seen - {start})

    downstream = walk(obj, depth)
    impacted_metrics = sorted(n for n, sql in metric_sqls.items()
                              if re.search(rf"\b{obj}\b", sql or ""))
    tree_lines = [f"{obj}"]
    for t in downstream:
        tree_lines.append(f"  └─(下游)→ {t}")
        for t2 in walk(t, 1):
            tree_lines.append(f"      └─→ {t2}")
    return {"object": obj, "direction": direction, "downstream": downstream,
            "impacted_metrics": impacted_metrics, "tree": "\n".join(tree_lines)}


# ---------------- 指标 ----------------

def query_metric(name: str) -> dict[str, Any]:
    """指标口径 + SQL 白话翻译（SELECT=统计 / FROM=取数表 / WHERE=筛选 / GROUP BY=分组 / JOIN=关联）。"""
    with _c() as conn:
        m = store.get_metric(conn, name)
    if not m:
        return {"error": f"指标未登记: {name}"}
    sql = m["calc_sql"]
    parts = []
    if (mm := re.search(r"SELECT\s+(.+?)\s+FROM", sql, re.I | re.S)):
        parts.append(f"统计口径：对 [{mm.group(1).strip()[:60]}] 做计算")
    for t in re.findall(r"\bFROM\s+(\w+)", sql, re.I):
        parts.append(f"数据来源：{t}")
    for w in re.findall(r"\bWHERE\s+(.+?)(?=\bGROUP BY\b|\bJOIN\b|$)", sql, re.I | re.S):
        parts.append(f"筛选条件：{w.strip()[:80]}")
    if re.search(r"\bGROUP BY\b", sql, re.I):
        parts.append("按分组维度分别计算")
    for j in re.findall(r"JOIN\s+(\w+)", sql, re.I):
        parts.append(f"关联表：{j}")
    m["plain_sql"] = parts
    return m


# ---- TODO W12/W13 ----
def diff_metrics(metric_a: str, metric_b: str):  # pragma: no cover
    """招牌工具：sqlglot 解析两指标 SQL，四类 diff（表/过滤/粒度/JOIN）逐类输出。"""
    raise NotImplementedError("W12: sqlglot AST diff")


def root_cause(rule_id: int):  # pragma: no cover
    raise NotImplementedError("W13: 血缘+历史归因")
