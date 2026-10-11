"""对话编排（规则版 v0.1）：意图识别 → 澄清 → 工具分发 → 审批确认流 → 回执。

W12 接 LLM 时只替换 classify()（提示词见 prompts/supervisor.md 的路由表），
handlers 与确认流不变——规则版同时是剧本测试的预期基线。
"""
from __future__ import annotations

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
    if re.search(r"帮助|你能干|怎么用|help|demo|能力", m):
        return "help"
    if re.search(r"flink|kafka|hdfs|hadoop|zookeeper|\bzk\b|flume|提交.*作业|topic|消费|作业列表", m):
        return "bigdata"
    if re.search(r"血缘|上下游|影响分析", m):
        return "lineage"
    if re.search(r"指标|口径", m):
        return "metric"
    if re.search(r"质量|校验规则|质检", m):
        return "quality"
    if re.search(r"标准|贯标|落标", m):
        return "compliance"
    if re.search(r"报送|映射|规范", m):
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
            rows = rpt.check_mapping("RPT_CUST_INFO")
            lines = ["✅ 报送映射检查：RPT_CUST_INFO（客户信息）", ""]
            miss = [r for r in rows if not r["src_field"]]
            for r in rows:
                src = f"{r['src_table']}.{r['src_field']}" if r["src_field"] else "❌ 未映射"
                lines.append(f"· {r['report_field']} ← {src}")
            if miss:
                lines += ["", f"⚠️ 缺失映射 {len(miss)} 个（规范必填）："]
                lines += [f"  - {r['report_field']}" for r in miss]
            lines += ["", "📚 出处：《客户信息报送规范-模拟》第二章（email 为 2026 年新增必填）"]
            return {"reply": "\n".join(lines)}
        # 规范问答：BM25 式关键词命中
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


# ---------------- 规范问答（BM25-lite） ----------------

def _ask_spec(msg: str) -> dict[str, Any]:
    import pathlib
    from ..config import SPEC_DOCS_DIR
    docs = list(pathlib.Path(SPEC_DOCS_DIR).glob("*.md"))
    best, score = None, 0
    for d in docs:
        text = d.read_text(encoding="utf-8")
        hits = sum(1 for ch in msg if '\u4e00' <= ch <= '\u9fff' and ch in text)
        words = [w for w in re.split(r"\s|，|。|？", msg) if len(w) >= 2]
        hits = sum(text.count(w) for w in words)
        if hits > score:
            best, score = d, hits
    if not best or score < 2:
        return {"reply": "规范库中未找到相关条文，建议人工确认。\n📚 已检索：《客户/账户/交易信息报送规范》3 篇（模拟）"}
    text = best.read_text(encoding="utf-8")
    # 命中条文段落（含小节号的行 ±2 行）
    lines = text.splitlines()
    picked, chapter = [], ""
    for i, ln in enumerate(lines):
        if re.match(r"## 第", ln):
            chapter = ln.strip("# ")
        for w in [x for x in re.split(r"\s|，|。|？", msg) if len(x) >= 2]:
            if w in ln and ln.strip() and ln not in picked:
                picked.append(ln.strip())
    body = "\n".join(picked[:4]) or "（命中章节但未提取到具体条文行）"
    return {"reply": f"✅ 规范问答（关键词命中 {score} 处）：\n\n{body}\n\n📚 出处：《{best.stem}》{chapter}"}


DEMO_FLINK_SQL = """INSERT INTO dm_channel_trx_rt
SELECT channel, COUNT(*) AS cnt, SUM(amt) FROM dwd_trx
WHERE trx_date = CURRENT_DATE GROUP BY channel"""

HELP_TEXT = """我是 DGA · 数据治理 Agent（v0.1 规则路由模式，LLM 编排 W12 接入）。试试这些：

**接入**
· 看看库里有什么表
· 看看 ods_customer_src（表画像 + PII 识别）

**治理**
· 检查 ods_customer_src 落标（贯标率 + 三层检查 + 推荐）
· 按 STD-C-003 给 CertNo 贯标（会出变更计划卡 → 确认）
· 跑一下 ods_customer_src 的质量规则（六性真跑数）
· 日活跃客户数的口径是什么（SQL 白话翻译）
· ods_customer_src 的血缘和影响分析

**报送**
· 检查客户信息报送表的映射
· 报送规范里证件类型的码值要求是什么

**大数据（Apache 组件，demo 模式）**
· 列一下 Kafka topic / 消费 dga.dq.alerts
· 把质量告警发到 Kafka（计划卡）
· 提交一个 Flink SQL 作业（计划卡 + DAG 预览）
· 看看 Flink 作业列表
· 列一下 HDFS /user/dga 目录
· 看看 ZooKeeper /dga 节点
· 生成一个 Flume 配置"""

FALLBACK_TEXT = "这个意图我还没学会（v0.1 规则路由模式）。说「帮助」看能力清单。"

BIGDATA_HELP = """大数据工具（demo 模式，接口语义与真实组件一致）：
· Kafka：列 topic / 消费 <topic> / 发送告警到 Kafka
· Flink：提交 Flink SQL 作业 / 作业列表
· HDFS：列目录 /user/dga / 读文件内容
· ZooKeeper：看 /dga 节点 / 注册数据源
· Flume：生成采集配置"""
