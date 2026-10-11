"""对话编排（规则版 v0.1）：意图识别 → 澄清 → 工具分发 → 审批确认流 → 回执。

W12 接 LLM 时只替换 classify()（提示词见 prompts/supervisor.md 的路由表），
handlers 与确认流不变——规则版同时是剧本测试的预期基线。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import META_DB
from ..meta import store
from ..tools import bigdata_tools as bd
from ..tools import govern_tools as gov
from ..tools import ingest_tools as ing
from ..tools import report_tools as rpt
from . import gate

# 会话内存状态：pending 审批 + 最近对象（粘性上下文）。demo 重启即失，可接受。
SESSIONS: dict[str, dict[str, Any]] = {}


def _sess(session: str) -> dict[str, Any]:
    return SESSIONS.setdefault(session, {"pending": None, "recent_table": None, "recent_metric": None})


# ---------------- 意图识别（LLM 替换点） ----------------

INTENTS = ("confirm", "cancel", "help", "probe", "compliance", "quality",
           "metric", "lineage", "report", "bigdata")


def classify(msg: str) -> str:
    m = msg.lower()
    if re.search(r"^确认$|^好$|^可以$|确认执行", m):
        return "confirm"
    if re.search(r"^取消$", m):
        return "cancel"
    if re.search(r"帮助|你能干|怎么用|help|能力", m):
        return "help"
    if re.search(r"flink|kafka|hdfs|hadoop|zookeeper|\bzk\b|flume|提交.*作业|topic|消费|作业列表", m):
        return "bigdata"
    if re.search(r"差在哪|口径差|对比.*口径|口径.*对比|diff", m):
        return "metric_diff"
    if re.search(r"登记.*指标|新增.*指标|注册.*指标", m):
        return "metric_register"
    if re.search(r"血缘|上下游|影响分析", m):
        return "lineage"
    if re.search(r"根因", m):
        return "root_cause"
    if re.search(r"退役", m):
        return "retire"
    if re.search(r"指标|口径", m):
        return "metric"
    if re.search(r"质量|质检", m):
        return "quality"
    if re.search(r"补描述|描述补全|无注释|字段.*描述", m):
        return "enrich"
    if re.search(r"分级|敏感等级|分类分级", m):
        return "level"
    if re.search(r"标准|贯标|落标", m):
        return "compliance"
    if re.search(r"试跑|对账", m):
        return "dry_run"
    if re.search(r"同步配置|抽取配置|datax|cdc|全量同步|增量同步", m):
        return "sync_config"
    if re.search(r"快照", m):
        return "snapshot"
    if re.search(r"连.*库|接入数据源|连接数据源", m):
        return "connect"
    if re.search(r"报送|映射|规范|取数|预览|校验规则|勾稽", m):
        return "report"
    if re.search(r"看看|探查|表结构|画像|有哪些表|连一下|什么表", m):
        return "probe"
    return "fallback"


def _extract_table(msg: str) -> str | None:
    known = {t["table"].lower(): t["table"] for t in ing.list_tables()}
    for token in re.findall(r"[A-Za-z_][\w]*", msg.lower()):
        if token in known:
            return known[token]
    # 中文消息里直接出现表名
    for low, orig in known.items():
        if low in msg.lower():
            return orig
    return None


def _extract_metric(msg: str) -> str | None:
    with store.connect(META_DB) as conn:
        names = [m["name"] for m in store.list_metrics(conn)]
    hit = [n for n in names if n and n in msg]
    return hit[0] if hit else None


def _extract_metrics2(msg: str) -> tuple[str, str] | None:
    """提取消息中出现的两个指标名（按出现顺序）。"""
    with store.connect(META_DB) as conn:
        names = [m["name"] for m in store.list_metrics(conn)]
    hit = [n for n in names if n and n in msg]
    hit = list(dict.fromkeys(hit))
    return (hit[0], hit[1]) if len(hit) >= 2 else None


# ---------------- handlers ----------------

def handle(intent: str, msg: str, session: str) -> dict[str, Any]:
    """返回 {reply, plan?, done}；plan 非空时前端渲染计划卡并等待确认。"""
    s = _sess(session)

    if intent == "confirm":
        return _confirm(s, session)
    if intent == "cancel":
        s["pending"] = None
        return {"reply": "已取消当前变更计划，回到对话。"}

    if intent == "help":
        return {"reply": HELP_TEXT}

    if intent == "probe":
        if "什么表" in msg or "有哪些表" in msg or not _extract_table(msg):
            if not _extract_table(msg) and "看看" in msg and "什么表" not in msg:
                tables = ", ".join(t["table"] for t in ing.list_tables())
                return {"reply": f"📋 库里有 8 张表：{tables}\n你想看哪张？（说表名即可，如：看看 ods_customer_src）"}
            tables = "\n".join(f"· {t['table']}（{t['rows']} 行）" for t in ing.list_tables())
            return {"reply": f"📋 bank_retail 共 {len(ing.list_tables())} 张表：\n{tables}\n\n➡️ 建议：看看 ods_customer_src（治理演示主角，有脏数据）"}
        table = _extract_table(msg)
        s["recent_table"] = table
        p = ing.profile_table(table)
        lines = [f"📋 表 {p['table']}（{p['rows']} 行，{len(p['columns'])} 字段）", ""]
        for c in p["columns"][:8]:
            flag = " ⚠️疑似PII" if c.get("suspected_pii") else ""
            top = f" top={','.join(map(str, c.get('top5', [])[:3]))}" if c.get("top5") else ""
            lines.append(f"· {c['col']}：{c['type']}，空值率 {c['null_rate']}{top}{flag}")
        lines += ["", "➡️ 建议下一步：检查这张表的落标情况（说：检查 ods_customer_src 落标）"]
        return {"reply": "\n".join(lines)}

    if intent == "compliance":
        table = _extract_table(msg) or s.get("recent_table")
        if not table:
            return {"reply": "要检查哪张表？候选：" + ", ".join(t["table"] for t in ing.list_tables()[:5]) + " …"}
        s["recent_table"] = table
        if re.search(r"给|按|绑定", msg):
            return _plan_bind(msg, table, s)
        r = gov.check_compliance(table)
        if "error" in r:
            return {"reply": r["error"]}
        s["pending_compliance"] = r
        lines = [f"✅ 落标检查完成：{table}",
                 f"📊 贯标率 {r['compliance_rate']*100:.0f}%（{r['bound']}/{r['total_fields']} 字段已贯标）", ""]
        for d in r["details"]:
            issues = []
            if not d["type_ok"]:
                issues.append("类型不兼容")
            if not d["naming_ok"]:
                issues.append("命名不符")
            if d["code_issues"]:
                issues.append(f"码值超出: {d['code_issues']['invalid_values']}")
            lines.append(f"· {d['col']} → {d['std_name']}" + (f" ⚠️ {'；'.join(issues)}" if issues else " ✅"))
        if r["recommendations"]:
            lines += ["", "🔍 未贯标字段推荐："]
            for rc in r["recommendations"][:5]:
                lines.append(f"· {rc['col']} → {rc['recommend_std']} {rc['recommend_name']}（置信度 {rc['confidence']}）")
        lines += ["", "➡️ 说「按 STD-C-003 给 CertNo 贯标」可执行绑定（会先出变更计划卡）"]
        return {"reply": "\n".join(lines)}

    if intent == "quality":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        r = gov.run_quality_rules(table)
        lines = [f"✅ 质量规则执行完成：{table}（{r['rows']} 行）",
                 f"📊 综合通过率 {r['overall_pass']*100:.1f}%", ""]
        for q in r["rules"]:
            mark = "✅" if q["fail_rows"] == 0 else "❌"
            lines.append(f"{mark} [{q['dim']}] {q['name']}：失败 {q['fail_rows']} 行（通过率 {q['pass_rate']*100:.1f}%）")
        lines += ["", "➡️ 建议下一步：把失败告警发到 Kafka（说：把质量告警发到 Kafka）"]
        return {"reply": "\n".join(lines)}

    # ---- v0.2 新增：口径 diff / 登记指标 / 根因 / 退役 / 描述 / 分级 ----
    if intent == "metric_diff":
        names = _extract_metrics2(msg) or (s.get("recent_pair") or
                                          (s.get("recent_metric"), None))
        if not names or not all(names):
            return {"reply": "要比对哪两个指标？例如：对公客户日均余额 和 零售客户日均余额 口径差在哪"}
        a, b = names
        s["recent_pair"] = names
        r = gov.diff_metrics(a, b)
        if "error" in r:
            return {"reply": r["error"]}
        lines = [f"✅ 口径比对完成（diff_metrics · sqlglot AST）",
                 f"📊 {a} ⚖️ {b}", ""]
        zh = {"tables": "数据来源", "filters": "过滤条件", "grain": "分组粒度", "joins": "关联方式"}
        for k, label in zh.items():
            d = r["diffs"][k]
            mark = "✅ 一致" if d["same"] else "❌ 不同"
            detail = "" if d["same"] else f" —— A: {d['a']}  vs  B: {d['b']}"
            lines.append(f"· {label}：{mark}{detail}")
        lines += ["", f"🔍 结论：{r['conclusion']}",
                  "➡️ 建议下一步：查询任一指标的完整口径（说：{0}的口径是什么）".format(a)]
        return {"reply": "\n".join(lines)}

    if intent == "metric_register":
        r = gov.register_metric(
            name=re.search(r"指标[叫:]?\s*([\w\u4e00-\u9fa5]+)", msg).group(1) if re.search(r"指标[叫:]?\s*([\w\u4e00-\u9fa5]+)", msg) else f"新指标{len(gov.list_standards())}",
            biz_text="对话登记（待补充业务定义）",
            calc_sql=re.search(r"(SELECT[\s\S]+)", msg, re.I).group(1) if re.search(r"(SELECT[\s\S]+)", msg, re.I) else "SELECT 1",
            owner="对话用户")
        return {"reply": f"✅ 指标登记结果：{json.dumps(r, ensure_ascii=False)}\n➡️ 可说「{r.get('name','新指标')}的口径是什么」验证"}

    if intent == "root_cause":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        r = gov.root_cause("最新失败规则", table)
        lines = [f"✅ 根因分析完成：{table}", ""]
        lines += [f"· {h}" for h in r["hypotheses"]]
        lines += ["", f"➡️ 验证建议：{r['verify']}"]
        return {"reply": "\n".join(lines)}

    if intent == "retire":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        r = gov.retire_rules(table)
        lines = [f"✅ 规则生命周期检查：{table}（现有 {r['total_rules']} 条 active）", ""]
        if r["retire_suggestion"]:
            lines.append("⚠️ 建议退役（连续全通过）：")
            lines += [f"  - #{x['id']} [{x['dim']}]" for x in r["retire_suggestion"]]
            plan = gate_plan(session, "retire_rules",
                             f"退役 {len(r['retire_suggestion'])} 条全通过规则",
                             {"rule_ids": [x["id"] for x in r["retire_suggestion"]]})
            return {"reply": "\n".join(lines) + "\n\n请确认计划卡执行退役。", "plan": plan}
        lines.append("暂无满足退役条件的规则（需连续全通过）。")
        return {"reply": "\n".join(lines)}

    if intent == "enrich":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        r = gov.enrich_description(table)
        if "error" in r:
            return {"reply": r["error"]}
        if not r["suggestions"]:
            return {"reply": f"✅ {table} 全部字段已有注释，无需补全。"}
        s["pending_enrich"] = r["suggestions"]
        lines = [f"✅ 描述补全建议（{table}，{r['no_comment']}/{r['total']} 字段无注释）：", ""]
        lines += [f"· {x['col']} → {x['suggest']}" for x in r["suggestions"]]
        plan = gate_plan(session, "enrich_description",
                         f"为 {table} 的 {r['no_comment']} 个字段写回描述",
                         {"table": table})
        return {"reply": "\n".join(lines) + "\n\n确认后写回（审计可回放）。", "plan": plan}

    if intent == "level":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        r = gov.classify_level(table)
        lines = [f"✅ 分类分级建议（{table}）：",
                 f"📊 建议等级：{r['level']}",
                 f"🔍 依据：{r['basis']}" + (f"；PII 字段：{('、'.join(r['pii_fields']))}" if r["pii_fields"] else "")]
        plan = gate_plan(session, "classify_level", f"{table} 定级为 {r['level']}", {"table": table, "level": r["level"]})
        return {"reply": "\n".join(lines) + "\n\n确认后写回表级标签。", "plan": plan}

    # ---- v0.2 新增：接入侧（连接/快照/同步配置/对账） ----
    if intent == "connect":
        from pathlib import Path as _P
        url = re.search(r"([\w:/\\.-]+\.duckdb)", msg)
        if not url:
            return {"reply": "给我一个 DuckDB 文件路径（v0.1 支持 duckdb；MySQL 在 W12）。示例：连接数据源 demo/bank_retail.duckdb"}
        r = ing.connect_datasource(url.group(1), f"src_{_P(url.group(1)).stem}")
        if "error" in r:
            return {"reply": f"❌ {r['error']}"}
        if r.get("already_registered"):
            return {"reply": f"ℹ️ 数据源 {r['name']} 已登记过（发现 {r['tables_found']} 张表），无需重复接入。\n➡️ 建议下一步：看看有什么表 / 做元数据快照"}
        return {"reply": f"✅ 数据源已登记：{r['name']}（发现 {r['tables_found']} 张表）\n➡️ 建议下一步：看看有什么表 / 做元数据快照"}

    if intent == "snapshot":
        table = _extract_table(msg) or s.get("recent_table")
        if not table:
            return {"reply": "要快照哪张表？（把 schema+画像登记进元数据库，治理工具才能操作它）"}
        plan = gate_plan(session, "snapshot_metadata",
                         f"元数据快照登记：{table}", {"table": table})
        return {"reply": f"快照 {table} 会把表结构+画像写入元数据库（幂等），请确认计划卡。", "plan": plan}

    if intent == "sync_config":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        mode = "increment" if re.search(r"增量", msg) else ("stream" if re.search(r"cdc|实时|流", msg) else "full")
        plan = gate_plan(session, "gen_sync_config",
                         f"生成同步配置：{table}（{mode}）", {"table": table, "mode": mode})
        s["pending_sync"] = {"table": table, "mode": mode}
        return {"reply": f"将生成 **{mode}** 模式同步配置（{table}），保存到 configs/。请确认计划卡。", "plan": plan}

    if intent == "dry_run":
        p = s.get("pending_sync") or {"table": _extract_table(msg) or s.get("recent_table") or "ods_customer_src", "mode": "full"}
        plan = gate_plan(session, "dry_run_check",
                         f"试跑对账：{p['table']}（limit 100）", {"table": p["table"]})
        return {"reply": f"试跑会建 `_dga_check_` 临时表（用完即删）比对条数与字段值。请确认计划卡。", "plan": plan}

    if intent == "metric":
        name = _extract_metric(msg) or s.get("recent_metric")
        if not name:
            with store.connect(META_DB) as conn:
                names = "、".join(m["name"] for m in store.list_metrics(conn)[:8])
            return {"reply": f"查哪个指标的口径？已登记指标：{names} …"}
        s["recent_metric"] = name
        m = gov.query_metric(name)
        if "error" in m:
            return {"reply": m["error"]}
        lines = [f"✅ 指标口径：{name}", f"📖 业务定义：{m['biz_text']}",
                 f"👤 负责人 {m['owner']} · 频率 {m['freq']} · 粒度 {m['grain']}", "", "🔧 计算逻辑（白话）："]
        lines += [f"{i+1}. {p}" for i, p in enumerate(m["plain_sql"])]
        lines += ["", f"```sql\n{m['calc_sql']}\n```"]
        return {"reply": "\n".join(lines)}

    if intent == "lineage":
        table = _extract_table(msg) or s.get("recent_table") or "ods_customer_src"
        r = gov.lineage(table)
        lines = [f"✅ 血缘与影响分析：{r['object']}", "", "```", r["tree"], "```"]
        if r["impacted_metrics"]:
            lines += ["", f"⚠️ 受影响指标：{('、'.join(r['impacted_metrics']))}"]
        return {"reply": "\n".join(lines)}

    if intent == "report":
        if re.search(r"映射", msg):
            r = rpt.check_mapping("RPT_CUST_INFO")
            if "error" in r:
                return {"reply": r["error"]}
            lines = [f"✅ 映射检查：RPT_CUST_INFO（{r['mapped']}/{r['required']} 已映射）", ""]
            if r["missing"]:
                lines.append(f"❌ 缺失映射 {len(r['missing'])} 个（规范必填）：")
                lines += [f"  - {f}" for f in r["missing"]]
            if r["type_issues"]:
                lines.append(f"⚠️ 类型不匹配 {len(r['type_issues'])} 个：")
                lines += [f"  - {t['field']}：{t['issue']}" for t in r["type_issues"]]
            if r["code_issues"]:
                lines.append(f"⚠️ 码值不一致 {len(r['code_issues'])} 个：")
                lines += [f"  - {c['field']}：源值 {c['invalid']} 超出 {c['std']}" for c in r["code_issues"]]
            if not (r["missing"] or r["type_issues"] or r["code_issues"]):
                lines.append("三类检查全部通过 ✅")
            lines += ["", f"📚 出处：《{r['spec']}》第二章"]
            return {"reply": "\n".join(lines)}
        if re.search(r"预览", msg):
            r = rpt.preview_report("RPT_CUST_INFO")
            if "error" in r:
                return {"reply": r["error"]}
            lines = [f"✅ 报送预览：RPT_CUST_INFO（抽样 {r['rows_previewed']}/{r['source_rows']} 行）", "",
                     "📊 必填字段完整率："]
            low = [(k, v) for k, v in r["completeness"].items() if v < 1]
            ok_n = len(r["completeness"]) - len(low)
            lines.append(f"· {ok_n}/{len(r['completeness'])} 字段 100%")
            lines += [f"· ❌ {k}：{v*100:.0f}%" for k, v in low]
            if r["missing_fields"]:
                lines.append(f"· ❌ 未映射不产出：{('、'.join(r['missing_fields']))}")
            lines += ["", "样例（PII 已脱敏）："]
            for row in r["sample"][:3]:
                lines.append("  " + json.dumps(row, ensure_ascii=False)[:150])
            return {"reply": "\n".join(lines)}
        if re.search(r"校验规则|勾稽", msg):
            rules = rpt.gen_check_rules("RPT_CUST_INFO")
            lines = ["✅ 报送校验规则生成：RPT_CUST_INFO（按《客户信息报送规范-模拟》）", ""]
            for r in rules:
                lines.append(f"· [{r['type']}] {r['rule']}")
            lines += ["", "```sql" + "\n-- 示例（码值类）\n" + next(r["sql"] for r in rules if r["type"] == "码值") + "\n```",
                      "➡️ 可直接执行验证（说：跑一下 ods_customer_src 质量规则）"]
            return {"reply": "\n".join(lines)}
        if re.search(r"取数|报送.*sql|生成.*报送.*(sql|语句)", msg) or re.search(r"生成.*报送", msg):
            r = rpt.gen_report_sql("RPT_CUST_INFO")
            return {"reply": f"✅ 报送去数 SQL 已生成：{r['file']}\n（{r['note']}）\n```sql\n{r['sql'][:600]}\n```"}
        return _ask_spec(msg)

    if intent == "bigdata":
        return _handle_bigdata(msg, session, s)

    return {"reply": FALLBACK_TEXT}


# ---------------- bigdata 子路由 ----------------

def _handle_bigdata(msg: str, session: str, s: dict) -> dict[str, Any]:
    m = msg.lower()
    if re.search(r"topic|主题", m) and re.search(r"列|list|有哪些|看看", m):
        rows = bd.kafka_topics()
        lines = ["✅ Kafka topic 列表（demo broker）："[:]]
        for r in rows:
            lines.append(f"· {r['topic']}：{r['messages']} 条，最新 {r['latest']}")
        lines += ["", "➡️ 说「消费 dga.dq.alerts」看告警消息"]
        return {"reply": "\n".join(lines)}
    if re.search(r"消费|consume|读.*消息", m):
        topic = next((t["topic"] for t in bd.kafka_topics() if t["topic"].lower() in m), "dga.dq.alerts")
        rows = bd.kafka_consume(topic)
        lines = [f"✅ 消费 {topic}（最近 {len(rows)} 条）:", ""]
        for r in rows:
            lines.append(f"[{r['ts']}] key={r['key']}  {r['value']}")
        return {"reply": "\n".join(lines)}
    if re.search(r"发.*kafka|发送.*消息|produce|告警.*kafka|kafka.*告警", m):
        plan = gate_plan(session, "kafka_produce",
                         f"发送质量告警到 dga.dq.alerts",
                         {"topic": "dga.dq.alerts"})
        return {"reply": "检测到「发送消息到 Kafka」——写操作，请先确认变更计划卡。", "plan": plan}
    if re.search(r"提交|submit|insert\s+into", m):
        sql_match = re.search(r"(insert\s+into[\s\S]+)", msg, re.I)
        sql = sql_match.group(1).strip() if sql_match else DEMO_FLINK_SQL
        s["pending_sql"] = sql
        plan = gate_plan(session, "flink_sql_submit", "提交 Flink SQL 作业", {"sql_head": sql[:60]})
        return {"reply": f"检测到 Flink SQL 作业提交：\n```sql\n{sql[:300]}\n```\n请确认变更计划卡后执行。", "plan": plan}
    if re.search(r"作业|job", m) and re.search(r"列|list|有哪些|看看", m):
        jobs = bd.flink_jobs()
        lines = ["✅ Flink 作业列表：" if jobs else "暂无作业。说「提交一个 Flink SQL 作业」试试"]
        for j in jobs:
            lines.append(f"· #{j['id']} {j['name']} [{j['status']}] {j['created_at']}")
        return {"reply": "\n".join(lines)}
    if re.search(r"hdfs|文件|目录", m):
        if re.search(r"cat|看.*内容", m):
            p = re.search(r"/[\w/.-]+", msg)
            return {"reply": "```\n" + bd.hdfs_cat(p.group(0) if p else "/user/dga/data/README") + "\n```"}
        p = re.search(r"/[\w/.-]*", msg)
        rows = bd.hdfs_ls(p.group(0) if p and p.group(0) != "/" else "/")
        lines = [f"✅ HDFS 目录 {p.group(0) if p else '/'}：" if p else "✅ HDFS / ："]
        lines += [f"· {r['type']:4} {r['path']}" + (f" ({r['size']}B)" if r["type"] == "file" else "") for r in rows]
        return {"reply": "\n".join(lines)}
    if re.search(r"zookeeper|zk", m):
        if re.search(r"注册|写入|set", m):
            plan = gate_plan(session, "zk_set", "注册数据源 znode 到 ZooKeeper",
                             {"path": "/dga/datasources/bank_retail", "value": "online"})
            return {"reply": "检测到 ZK 注册——写操作，请确认计划卡。", "plan": plan}
        rows = bd.zk_get("/dga")
        lines = ["✅ ZooKeeper /dga：", f"  value: {rows['value']}", "  children:"]
        lines += [f"  · {c}" for c in rows["children"]]
        return {"reply": "\n".join(lines)}
    if re.search(r"flume", m):
        r = bd.flume_gen_config()
        return {"reply": f"✅ Flume agent 配置已生成：{r['config_file']}（{r['lines']} 行）\n```properties\n{r['preview']}\n```"}
    return {"reply": BIGDATA_HELP}


# ---------------- 审批 ----------------

def gate_plan(session: str, tool: str, action: str, extra: dict) -> dict[str, Any]:
    with store.connect(META_DB) as conn:
        actions = [{"tool": tool, "action": action, **extra,
                    "impact": "写入 demo 环境（Kafka 模拟/HDFS 本地/作业注册表）",
                    "rollback": "审计日志可回放；kafka/作业可删除记录"}]
        pid = gate.create_plan(conn, actions)
    SESSIONS.setdefault(session, {})["pending"] = {"plan_id": pid, "tool": tool, "extra": extra}
    return {"plan_id": pid, "title": action, "actions": actions}


def _plan_bind(msg: str, table: str, s: dict) -> dict[str, Any]:
    std = re.search(r"(STD-[A-Z]-\d+)", msg.upper())
    col = re.search(r"给\s+(\w+)\s+贯标", msg)
    if not (std and col):
        return {"reply": "格式：按 STD-C-003 给 CertNo 贯标（标准号 + 字段名）"}
    with store.connect(META_DB) as conn:
        actions = [{"tool": "bind_compliance",
                    "action": f"{table}.{col.group(1)} 贯标 → {std.group(1)}",
                    "impact": "改变落标检查结果与贯标率", "rollback": "解绑即恢复"}]
        pid = gate.create_plan(conn, actions)
    s["pending"] = {"plan_id": pid, "tool": "bind_compliance",
                    "extra": {"table": table, "col": col.group(1), "std": std.group(1)}}
    return {"reply": "请确认变更计划卡后执行贯标绑定。",
            "plan": {"plan_id": pid, "title": f"{table}.{col.group(1)} → {std.group(1)}",
                     "actions": actions}}


def _confirm(s: dict, session: str) -> dict[str, Any]:
    p = s.get("pending")
    if not p:
        return {"reply": "当前没有待确认的变更计划。"}
    tool, extra, pid = p["tool"], p["extra"], p["plan_id"]
    with store.connect(META_DB) as conn:
        gate.confirm_plan(conn, pid)
        if tool == "bind_compliance":
            tid = store.find_table(conn, 1, "main", extra["table"])["id"]
            store.bind_compliance(conn, tid, extra["col"], extra["std"])
            store.append_audit(conn, session, "govern", "bind_compliance",
                               f"{extra['table']}.{extra['col']}→{extra['std']}", pid, "ok")
            r = gov.check_compliance(extra["table"])
            s["pending"] = None
            return {"reply": f"✅ 已贯标：{extra['table']}.{extra['col']} → {extra['std']}（审计 plan={pid}）\n"
                             f"📊 贯标率更新为 {r['compliance_rate']*100:.0f}%（{r['bound']}/{r['total_fields']}）\n"
                             f"➡️ 说「检查 {extra['table']} 落标」看最新明细"}
        store.append_audit(conn, session, "bigdata", tool, str(extra), pid, "ok")
    # ---- v0.2 新工具的确认执行 ----
    if tool == "enrich_description":
        table = extra["table"]
        items = s.get("pending_enrich") or []
        n = gov.write_descriptions(table, [{"col": x["col"], "comment": x["suggest"]} for x in items])
        s["pending"] = None
        return {"reply": f"✅ 已写回 {n} 个字段的业务描述（审计 plan={pid}）\n"
                         f"➡️ 说「看看 {table}」或「检查 {table} 落标」验证效果"}
    if tool == "classify_level":
        gov.write_level(extra["table"], extra["level"])
        s["pending"] = None
        return {"reply": f"✅ {extra['table']} 已定级为 {extra['level']}（写回表级标签，审计 plan={pid}）"}
    if tool == "snapshot_metadata":
        r = ing.snapshot_metadata(extra["table"])
        s["pending"] = None
        return {"reply": f"✅ 元数据快照完成：{r['table']}（{r['fields']} 字段，新增登记 {r['new_fields_registered']}，行数 {r['rows']}）\n"
                         f"➡️ 治理工具现在可以操作它了：检查落标 / 跑质量规则"}
    if tool == "gen_sync_config":
        p = s.get("pending_sync") or extra
        r = ing.gen_sync_config(p["table"], p["mode"])
        s["pending"] = None
        if "error" in r:
            return {"reply": f"❌ {r['error']}"}
        return {"reply": f"✅ 同步配置已生成：{r['config_file']}（{r['kind']}，{r['columns']} 列）\n"
                         f"```json\n{r['preview'][:300]}\n```\n"
                         f"➡️ 建议下一步：试跑对账（说：试跑对账）"}
    if tool == "dry_run_check":
        r = ing.dry_run_check(extra["table"])
        s["pending"] = None
        ok = r["rows_match"] and r["values_match"]
        return {"reply": f"{'✅ 试跑对账通过' if ok else '❌ 对账不平'}：{r['source']}（抽样 {r['sampled']} 行）\n"
                         f"📊 条数一致：{'✅' if r['rows_match'] else '❌'}；字段值一致：{'✅' if r['values_match'] else '❌'}"
                         f"（{('、'.join(r['fields_checked']))}）\n"
                         f"🔍 临时表 {r['temp_table']}"}
    if tool == "retire_rules":
        n = gov.do_retire(extra.get("rule_ids", []))
        s["pending"] = None
        return {"reply": f"✅ 已退役 {n} 条规则（status=retired，审计 plan={pid}）\n"
                         f"➡️ 规则历史保留，可随时复活"}
    if tool == "kafka_produce":
        r = bd.kafka_produce("dga.dq.alerts", "rule-105",
                             '{"rule":"cert_type 码值超出标准","table":"ods_customer_src","level":"WARN"}')
        s["pending"] = None
        return {"reply": f"✅ 已发送到 {r['topic']}（key=rule-105，topic 现有 {r['topic_size']} 条）\n"
                         f"🔍 证据：审计 plan={pid}\n➡️ 说「消费 dga.dq.alerts」验证消息"}
    if tool == "flink_sql_submit":
        sql = s.get("pending_sql") or DEMO_FLINK_SQL
        r = bd.flink_sql_submit(sql, session=session)
        s["pending"] = None
        return {"reply": f"✅ Flink 作业已提交：#{r['job_id']} [{r['status']}]\n"
                         f"```{r['dag']}```\n🔍 计划：{r['plan']['parse']}\n➡️ 说「看看 Flink 作业列表」"}
    if tool == "zk_set":
        r = bd.zk_set("/dga/datasources/bank_retail", "online", session=session)
        s["pending"] = None
        return {"reply": f"✅ ZooKeeper 已注册：{r['path']} = {r['value']}"}
    s["pending"] = None
    return {"reply": f"计划 {pid} 已确认，但 {tool} 尚未接入（W12）。"}


# ---------------- 规范问答（report_tools.ask_spec） ----------------

def _ask_spec(msg: str) -> dict[str, Any]:
    r = rpt.ask_spec(msg)
    if not r.get("found"):
        return {"reply": r["message"]}
    lines = [f"✅ 规范问答（命中 {r['hits']} 处）：", ""]
    lines += [f"· {c}" for c in r["clauses"]]
    lines += ["", r["source"]]
    return {"reply": "\n".join(lines)}


DEMO_FLINK_SQL = """INSERT INTO dm_channel_trx_rt
SELECT channel, COUNT(*) AS cnt, SUM(amt) FROM dwd_trx
WHERE trx_date = CURRENT_DATE GROUP BY channel"""

HELP_TEXT = """我是 DGA · 数据治理 Agent（v0.2 全功能 · 规则路由模式，LLM 编排 W12 接入）。

**接入（6 工具全）**
· 看看库里有什么表 / 看看 ods_customer_src（画像+PII）
· 连接数据源 demo/bank_retail.duckdb
· 给 dim_product 做元数据快照（登记后可治理）
· 生成 ods_customer_src 的全量/增量同步配置（DataX）/ CDC 配置（Flink SQL）
· 试跑对账（临时表 + 条数/字段值双向核对）

**治理·元数据**
· 给 ods_customer_src 补字段描述（词典生成 → 计划卡 → 写回）
· 给 ods_customer_src 分级（PII 驱动 C2/C3/C4）

**治理·标准**
· 检查 ods_customer_src 落标（三层：类型/命名/码值 + 推荐置信度）
· 按 STD-C-003 给 CertNo 贯标（计划卡）

**治理·质量**
· 跑一下 ods_customer_src 质量规则（六性真跑数）
· ods_customer_src 根因分析（血缘上游归因）
· ods_customer_src 规则退役检查（全通过规则建议退役）

**治理·指标**
· 日活跃客户数的口径是什么（SQL 白话翻译）
· 对公客户日均余额和零售客户日均余额 口径差在哪（sqlglot 四类 diff·招牌）
· ods_customer_src 的血缘和影响分析

**报送（5 工具全）**
· 检查客户信息报送表的映射（缺失/类型/码值三类）
· 预览客户信息报送表（完整率+脱敏样例）
· 生成客户信息报送表的取数 SQL
· 报送规范里证件类型的码值要求是什么（带出处问答）

**大数据（Apache 组件，demo 模式）**
· Kafka：列 topic / 消费 dga.dq.alerts / 发送告警
· Flink：提交 SQL 作业（DAG 预览）/ 作业列表
· HDFS：列目录 / 读文件 · ZooKeeper：看节点 / 注册 · Flume：生成配置"""

FALLBACK_TEXT = "这个意图我还没学会（v0.1 规则路由模式）。说「帮助」看能力清单。"

BIGDATA_HELP = """大数据工具（demo 模式，接口语义与真实组件一致）：
· Kafka：列 topic / 消费 <topic> / 发送告警到 Kafka
· Flink：提交 Flink SQL 作业 / 作业列表
· HDFS：列目录 /user/dga / 读文件内容
· ZooKeeper：看 /dga 节点 / 注册数据源
· Flume：生成采集配置"""
