"""v0.2 新功能全量剧本测试：提示词定义的每个能力至少一条可执行路径。"""
import json
import sqlite3

import pytest

from dga.config import CONFIGS_DIR, META_DB
from dga.graph import router
from dga.tools import bigdata_tools as bd
from dga.tools import govern_tools as gov
from dga.tools import ingest_tools as ing
from dga.tools import report_tools as rpt


@pytest.fixture(scope="module", autouse=True)
def env():
    bd.init_bigdata_env()
    router.SESSIONS.clear()
    yield


def R(msg):
    return router.handle(router.classify(msg), msg, "t2")


# ---- 接入四件 ----
def test_connect_datasource():
    conn = sqlite3.connect(META_DB)                    # 幂等：清掉历史测试登记
    conn.execute("DELETE FROM datasource WHERE name LIKE 'src_%'")
    conn.commit()
    conn.close()
    r = R("连接数据源 demo/bank_retail.duckdb")
    assert "已登记" in r["reply"] or "已存在" in r["reply"] or "试连失败" in r["reply"]


def test_snapshot_flow():
    r = R("给 dim_product 做元数据快照")
    assert r.get("plan")
    r2 = R("确认")
    assert "快照完成" in r2["reply"] and "dim_product" in r2["reply"]


def test_sync_config_flow():
    r = R("生成 ods_customer_src 的全量同步配置")
    assert r.get("plan") and "full" in r["reply"]
    r2 = R("确认")
    assert "配置已生成" in r2["reply"]
    files = list((CONFIGS_DIR).glob("sync_ods_customer_src_full.json"))
    assert files and "querySql" in files[0].read_text(encoding="utf-8")


def test_sync_cdc():
    r = R("生成 ods_customer_src 的 CDC 流式同步配置")
    R("确认")
    assert list((CONFIGS_DIR).glob("sync_ods_customer_src_stream.sql"))


def test_dry_run_flow():
    r = R("试跑对账")
    assert r.get("plan")
    r2 = R("确认")
    assert "对账通过" in r2["reply"] and "字段值一致" in r2["reply"]


# ---- 元数据两件 ----
def test_enrich_flow():
    r = R("给 ods_customer_src 补字段描述")
    assert r.get("plan") and "CertNo" in r["reply"]
    r2 = R("确认")
    assert "已写回" in r2["reply"]
    # 幂等恢复：清掉写回的注释，避免污染其他测试
    conn = sqlite3.connect(META_DB)
    conn.execute("UPDATE meta_column SET comment=NULL WHERE table_id=(SELECT id FROM meta_table WHERE table_name='ods_customer_src') AND col_name IN ('CertNo','EmailAddr')")
    conn.commit()
    conn.close()


def test_level_flow():
    r = R("给 ods_customer_src 分级")
    assert r.get("plan") and "C2" in r["reply"]
    r2 = R("确认")
    assert "C2" in r2["reply"] and "已定级" in r2["reply"]


# ---- 质量：根因/退役 ----
def test_root_cause():
    r = R("ods_customer_src 根因分析")
    assert "根因分析完成" in r["reply"] and "上游" in r["reply"]


def test_retire_flow():
    R("跑一下 dim_customer 质量规则")   # 有全通过规则吗？dim_customer 干净表→六性大多通过
    r = R("dim_customer 规则退役检查")
    assert "规则生命周期检查" in r["reply"]
    if r.get("plan"):                    # 有可退役规则则走确认
        r2 = R("确认")
        assert "退役" in r2["reply"]


# ---- 指标：diff（招牌）+ 登记 ----
def test_diff_metrics_star():
    r = R("对公客户日均余额和零售客户日均余额 口径差在哪")
    assert "口径比对完成" in r["reply"]
    assert "过滤条件" in r["reply"] and "cust_type" in r["reply"]
    assert "仅过滤条件不同" in r["reply"]          # 结论正确：同表同粒度同JOIN
    assert "数据来源：✅ 一致" in r["reply"]


def test_diff_metrics_same_conclusion():
    r = gov.diff_metrics("对公客户数", "零售客户数")
    assert r["changed_dims"] == ["filters"]


def test_register_metric():
    conn = sqlite3.connect(META_DB)                    # 幂等：先清测试指标
    conn.execute("DELETE FROM metric_def WHERE name LIKE '测试指标%' OR name='坏SQL指标'")
    conn.commit()
    conn.close()
    r = gov.register_metric("测试指标-当日AUM", "当日账户总余额", "SELECT SUM(balance) FROM dwd_account", "测试")
    assert r.get("ok")
    m = gov.get_metric("测试指标-当日AUM")
    assert m and "dwd_account" in m["calc_sql"]


def test_register_bad_sql_rejected():
    r = gov.register_metric("坏SQL指标", "x", "SELEC bad sql ((")
    assert "error" in r


# ---- 报送三件+码值层 ----
def test_mapping_three_layers():
    r = R("检查客户信息报送表的映射")
    assert "缺失映射" in r["reply"] and "email" in r["reply"]      # ①缺失
    assert "码值不一致" in r["reply"] and "'9'" in r["reply"]      # ③码值（CERTYPE=9 超标）


def test_preview_report_masks_pii():
    r = R("预览客户信息报送表")
    assert "完整率" in r["reply"]
    assert "cert_no" in r["reply"]
    assert "***" in r["reply"]                                     # PII 脱敏


def test_gen_report_sql():
    r = R("生成客户信息报送表的取数 SQL")
    assert "INSERT INTO RPT_CUST_INFO" in r["reply"] and "未执行" in r["reply"]


def test_check_rules():
    r = R("按规范生成客户信息报送表的校验规则")
    assert "勾稽" in r["reply"] and "码值" in r["reply"]


# ---- 提示词红线 ----
def test_spec_no_hallucination():
    r = R("报送规范对量化投资杠杆率的要求是什么")
    assert "未找到" in r["reply"]                                   # 规范库没有→拒答


@pytest.fixture(scope="session", autouse=True)
def _cleanup_metrics():
    """会话级收尾：清掉测试登记的指标，保持种子计数稳定。"""
    yield
    conn = sqlite3.connect(META_DB)
    conn.execute("DELETE FROM metric_def WHERE name LIKE '测试指标%' OR name='坏SQL指标'")
    conn.commit()
    conn.close()
