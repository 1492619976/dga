"""对话编排（规则版 router）剧本测试——方案 7.2 的 S-01~S-08 落地版。"""
import pytest

from dga.config import META_DB
from dga.graph import router


@pytest.fixture(scope="module", autouse=True)
def env():
    from dga.tools.bigdata_tools import init_bigdata_env
    init_bigdata_env()
    router.SESSIONS.clear()
    yield


def R(msg):
    return router.handle(router.classify(msg), msg, "test")


# S-01 列表
def test_list_tables():
    r = R("看看库里有什么表")
    assert "ods_customer_src" in r["reply"]


# S-03 粘性上下文：先探查再落标，第二句不带表名
def test_sticky_context():
    R("看看 ods_customer_src")
    r = R("检查落标")            # 无表名 → 用 recent_table
    assert "贯标率" in r["reply"] or "落标检查完成" in r["reply"]


# S-04 落标检查含推荐与码值问题
def test_compliance_details():
    r = R("检查 ods_customer_src 落标")
    assert "码值超出" in r["reply"]            # CERTYPE='9' 被三层检查抓到
    assert "STD-" in r["reply"]                # 未贯标字段推荐


# S-05 贯标计划卡 → 确认 → 贯标率变化（幂等：先重置 MobilePhone 的贯标状态）
def test_bind_flow():
    import sqlite3
    conn = sqlite3.connect(META_DB)
    conn.execute("UPDATE meta_column SET compliance_id=NULL WHERE col_name='MobilePhone'")
    conn.commit()
    conn.close()
    r = R("按 STD-C-004 给 MobilePhone 贯标")
    assert r.get("plan") and r["plan"]["plan_id"]
    before = R("检查 ods_customer_src 落标")["reply"]
    r2 = R("确认")
    assert "已贯标" in r2["reply"]
    after = R("检查 ods_customer_src 落标")["reply"]
    import re as _re
    ra = int(_re.search(r"(\d+)%", after).group(1))
    rb = int(_re.search(r"(\d+)%", before).group(1))
    assert ra > rb


# S-06 指标口径白话翻译
def test_metric_plain():
    r = R("日活跃客户数的口径是什么")
    assert "业务定义" in r["reply"] and "SELECT" in r["reply"]


# 质量
def test_quality_runs():
    r = R("跑一下 ods_customer_src 质量规则")
    assert "综合通过率" in r["reply"] and "有效性" in r["reply"]


# 血缘
def test_lineage():
    r = R("ods_customer_src 的血缘和影响分析")
    assert "dim_customer" in r["reply"]          # 下游第一跳
    assert "受影响指标" in r["reply"] or "血缘" in r["reply"]


# 报送映射
def test_report_mapping():
    r = R("检查客户信息报送表的映射")
    assert "email" in r["reply"] and "未映射" in r["reply"]


# bigdata：kafka → flink
def test_kafka_and_flink_flow():
    r = R("列一下 Kafka topic")
    assert "dga.dq.alerts" in r["reply"]
    r = R("把质量告警发到 Kafka")
    assert r.get("plan")
    r2 = R("确认")
    assert "已发送" in r2["reply"]
    r3 = R("消费 dga.dq.alerts")
    assert "rule-105" in r3["reply"]
    r = R("提交一个 Flink SQL 作业")
    assert r.get("plan")
    r2 = R("确认")
    assert "Flink 作业已提交" in r2["reply"] and "#" in r2["reply"]


# S-07 取消流
def test_cancel():
    R("按 STD-C-005 给 EmailAddr 贯标")
    r = R("取消")
    assert "已取消" in r["reply"]


# 澄清
def test_clarify_when_no_table():
    r = R("检查落标")            # test 会话已有粘性表——新会话验证澄清
    router.SESSIONS.pop("fresh", None)
    router.SESSIONS.setdefault("fresh", {"pending": None, "recent_table": None, "recent_metric": None})
    r2 = router.handle("compliance", "检查落标", "fresh")
    assert "要检查哪张表" in r2["reply"]
