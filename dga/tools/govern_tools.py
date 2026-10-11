"""治理工具（14 个，四技能包）——v0.1 已实现：标准查询/落标检查/质量执行/血缘/指标查询。

元数据：enrich_description(TODO) / classify_pii(画像侧已有) / classify_level(TODO)
标准：  list_standards ✅ / recommend_standards(并入 check_compliance 输出) /
        check_compliance ✅ / bind_compliance(经 gate，store 已有)
质量：  run_quality_rules ✅（六性真跑） / gen_quality_rules ✅（模板） / root_cause(TODO)
指标：  query_metric ✅ / diff_metrics(TODO W12 sqlglot 招牌) / lineage ✅ / register_metric(TODO)
"""
from __future__ import annotations

import difflib
import json
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
                try:
                    fails = biz.execute(check(r["sql"])).fetchone()[0]
                except Exception:  # 列不存在（模板按 ods 定制）→ 跳过该规则
                    continue
                results.append({**r, "fail_rows": fails,
                                "pass_rate": round((total - fails) / max(total, 1), 4)})
                rid = store.save_dq_rule(conn, tid, r["dim"], r["sql"])
                conn.execute("UPDATE dq_rule SET last_result=? WHERE id=?",
                             (json.dumps({"fail_rows": fails, "pass_rate":
                                          round((total - fails) / max(total, 1), 4)},
                                         ensure_ascii=False), rid))
                conn.commit()
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
            if direction == "upstream":
                nxt = [a for a, b in edges if b in frontier and a not in seen]
            else:
                nxt = [b for a, b in edges if a in frontier and b not in seen]
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


# ---- 元数据包：描述补全（词典版） / 分级 ----

_FIELD_DICT = {
    "cust": "客户", "customer": "客户", "cert": "证件", "type": "类型", "no": "号码",
    "num": "编号", "name": "姓名", "mobile": "手机", "phone": "手机", "tel": "电话",
    "email": "电子邮箱", "mail": "邮箱", "addr": "联系地址", "address": "地址", "home": "居住",
    "open": "开户", "dt": "日期", "date": "日期", "time": "时间", "upd": "更新", "update": "更新",
    "sts": "状态", "status": "状态", "bal": "余额", "balance": "余额", "amt": "金额", "amount": "金额",
    "organ": "机构", "org": "机构", "channel": "渠道", "currency": "币种", "risk": "风险",
    "prod": "产品", "product": "产品", "trx": "交易", "trade": "交易", "trans": "交易",
    "acct": "账户", "account": "账户", "gender": "性别", "sex": "性别", "id": "标识",
    "src": "来源", "code": "编码", "level": "等级", "cnt": "数量", "count": "数量",
}


def _suggest_desc(col_name: str) -> str:
    tokens = re.split(r"[_\s]|(?<=[a-z])(?=[A-Z])", col_name)
    parts = [_FIELD_DICT.get(t.lower(), "") for t in tokens if t]
    words = [p for p in parts if p]
    if not words:
        return f"（待人工确认）{col_name}"
    return "".join(dict.fromkeys(words))  # 去重保序


def enrich_description(table: str) -> dict[str, Any]:
    """无注释字段生成中文描述建议（v0.2 词典版；W12 换 LLM 依据样例值生成）。"""
    with _c() as conn:
        t = store.find_table(conn, 1, "main", table)
        if not t:
            return {"error": f"元数据库未登记表: {table}"}
        cols = store.columns_of(conn, t["id"])
    suggestions = [{"col": c["col_name"], "suggest": _suggest_desc(c["col_name"])}
                   for c in cols if not c["comment"]]
    return {"table": table, "total": len(cols), "no_comment": len(suggestions),
            "suggestions": suggestions}


def write_descriptions(table: str, items: list[dict], session: str = "web") -> int:
    """审批后写回描述。items=[{col, comment}]。"""
    with _c() as conn:
        tid = store.find_table(conn, 1, "main", table)["id"]
        n = 0
        for it in items:
            n += store.update_column_comment(conn, tid, it["col"], it["comment"])
        store.append_audit(conn, session, "govern", "enrich_description", f"{table}:{len(items)}", None, "ok")
    return n


def classify_level(table: str) -> dict[str, Any]:
    """表级分类分级：C2 敏感(含PII) / C3 内部 / C4 公开。"""
    from . import ingest_tools as ing
    p = ing.profile_table(table)
    pii_cols = [c["col"] for c in p["columns"] if c.get("suspected_pii")]
    level = "C2-敏感" if pii_cols else ("C4-公开" if table.startswith("dim_") else "C3-内部")
    return {"table": table, "pii_fields": pii_cols, "level": level,
            "basis": "命中 PII 字段 → C2" if pii_cols else "无 PII 命中，按业务域定级"}


def write_level(table: str, level: str, session: str = "web") -> None:
    with _c() as conn:
        t = store.find_table(conn, 1, "main", table)
        conn.execute("UPDATE meta_table SET pii_level=? WHERE id=?", (level, t["id"]))
        conn.commit()
        store.append_audit(conn, session, "govern", "classify_level", f"{table}:{level}", None, "ok")


# ---- 质量包：根因 / 退役 ----

def root_cause(rule_name: str, table: str) -> dict[str, Any]:
    """根因分析：血缘上游 + 规则语义 → 归因假设 + 验证建议（v0.2 启发式）。"""
    up = lineage(table, direction="upstream")
    # upstream = walk 的另一向：直接从 edges 反查
    with _c() as conn:
        etls = store.list_etl_sql(conn)
    feeders = [e["name"] for e in etls if e["target_table"] == table]
    hypotheses = [
        f"规则【{rule_name}】在 {table} 上失败，可能来源：",
        f"① 上游供给：该表由 ETL 任务 {('、'.join(feeders)) or '（无登记任务，直连源）'} 供给，"
        f"上游表 {('、'.join(up.get('downstream') or [])) or '（无登记上游）'} 的字段质量未验证",
        "② 源头录入：若为 ods 源表，脏值通常来自业务系统录入环节（如码值 '9' 为系统默认/未知态）",
        "③ 时间窗口：若失败集中在近期批次，检查增量同步的 where 条件与断点",
    ]
    return {"rule": rule_name, "table": table, "hypotheses": hypotheses,
            "verify": f"对上游执行质量规则（说：跑一下 {up.get('downstream') or ['dim_customer'][0]} 质量规则）"}


def retire_rules(table: str, session: str = "web") -> dict[str, Any]:
    """连续全通过规则建议退役；返回建议清单（执行写状态须过审批）。"""
    with _c() as conn:
        t = store.find_table(conn, 1, "main", table)
        rows = store.rows(conn.execute(
            "SELECT id, dim, expr_sql, last_result, status FROM dq_rule WHERE table_id=?",
            (t["id"],)))
    to_retire = [r for r in rows if r["status"] == "active"
                 and r["last_result"] and '"fail_rows": 0' in r["last_result"]]
    keep = [r for r in rows if r not in to_retire]
    return {"table": table, "total_rules": len(rows),
            "retire_suggestion": [{"id": r["id"], "dim": r["dim"]} for r in to_retire],
            "keep": len(keep)}


def do_retire(rule_ids: list[int], session: str = "web") -> int:
    with _c() as conn:
        n = 0
        for rid in rule_ids:
            cur = conn.execute("UPDATE dq_rule SET status='retired' WHERE id=? AND status='active'", (rid,))
            n += cur.rowcount
        conn.commit()
        store.append_audit(conn, session, "govern", "retire_rules", str(rule_ids), None, "ok")
    return n


# ---- 指标包：口径 diff（sqlglot 招牌） / 登记 ----

def diff_metrics(metric_a: str, metric_b: str) -> dict[str, Any]:
    """sqlglot 解析两指标 SQL AST，四类差异逐类输出 + 一句话结论。"""
    import sqlglot
    from sqlglot import exp

    ma, mb = get_metric(metric_a), get_metric(metric_b)
    if not ma or not mb:
        return {"error": f"指标未登记: {ma is None and metric_a or metric_b}"}
    try:
        ea = sqlglot.parse_one(ma["calc_sql"])
        eb = sqlglot.parse_one(mb["calc_sql"])
    except Exception as e:  # noqa: BLE001
        return {"error": f"SQL 解析失败: {e}"}

    def tables(e):
        return sorted({t.name for t in e.find_all(exp.Table)})

    def where(e):
        w = e.args.get("where")
        return w.sql(dialect="duckdb") if w else "(无)"

    def group(e):
        g = e.args.get("group")
        return g.sql(dialect="duckdb") if g else "(全量汇总)"

    def joins(e):
        return sorted(j.sql(dialect="duckdb") for j in e.find_all(exp.Join))

    ta, tb = tables(ea), tables(eb)
    diffs = {
        "tables": {"same": ta == tb, "a": ta, "b": tb},
        "filters": {"same": where(ea) == where(eb), "a": where(ea), "b": where(eb)},
        "grain": {"same": group(ea) == group(eb), "a": group(ea), "b": group(eb)},
        "joins": {"same": joins(ea) == joins(eb), "a": joins(ea), "b": joins(eb)},
    }
    changed = [k for k, v in diffs.items() if not v["same"]]
    if not changed:
        conclusion = "两指标口径完全一致"
    elif changed == ["filters"]:
        conclusion = f"两口径仅过滤条件不同（{metric_a} vs {metric_b}）"
    else:
        conclusion = f"两口径差异类型：{'、'.join(changed)}"
    return {"metric_a": metric_a, "metric_b": metric_b, "diffs": diffs,
            "changed_dims": changed, "conclusion": conclusion}


def register_metric(name: str, biz_text: str, calc_sql: str, owner: str = "数据部",
                    freq: str = "日", session: str = "web") -> dict[str, Any]:
    """登记新指标：sqlglot 先校验可解析 → 写入（调用方须已过审批）。"""
    import sqlglot
    try:
        sqlglot.parse_one(calc_sql)
    except Exception as e:  # noqa: BLE001
        return {"error": f"SQL 不可解析，拒绝登记: {e}"}
    with _c() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO metric_def(name,biz_text,calc_sql,owner,freq,grain,registered_at) "
                "VALUES (?,?,?,?,?,?,datetime('now'))",
                (name, biz_text, calc_sql, owner, freq, "待定"))
            conn.commit()
        except Exception as e:  # noqa: BLE001
            return {"error": f"登记失败（重名？）: {e}"}
        store.append_audit(conn, session, "govern", "register_metric", name, None, "ok")
        return {"id": cur.lastrowid, "name": name, "ok": True}


# ---- 旧 TODO 清理 ----
