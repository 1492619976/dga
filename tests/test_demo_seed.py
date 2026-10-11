"""demo 种子完整性：跑 build_demo 后的元数据断言（发布前必绿）。"""
import sqlite3

import pytest

from dga.config import BUSINESS_DB, META_DB


@pytest.fixture(scope="module", autouse=True)
def built():
    if not META_DB.exists() or not BUSINESS_DB.exists():
        import demo.build_demo as bd
        bd.build_business_db()
        bd.build_meta_db()
    yield


def test_seed_counts():
    conn = sqlite3.connect(META_DB)
    conn.row_factory = sqlite3.Row
    # 幂等自愈：剧本测试会登记测试数据源/指标，断言前恢复种子状态
    conn.execute("DELETE FROM datasource WHERE id > 1")
    conn.execute("DELETE FROM metric_def WHERE name LIKE '测试指标%' OR name='坏SQL指标'")
    conn.commit()
    for table, n in [("gov_standard", 20), ("metric_def", 15), ("etl_sql", 10),
                     ("report_mapping", 9), ("datasource", 1)]:
        got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert got == n, f"{table}: expect {n}, got {got}"
    conn.close()


def test_dirty_table_compliance_gap():
    """脏源表 12 字段、贯标 3 个 → 贯标率 25%（落标检查演示位）。

    幂等：router 剧本测试会往这张表写贯标关系，断言前先恢复种子绑定状态。
    """
    from demo.build_demo import ODS_COLS
    conn = sqlite3.connect(META_DB)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("SELECT id FROM meta_table WHERE table_name='ods_customer_src'").fetchone()[0]
    seed_bind = {name: std for name, _t, _c, _n, _p, std in ODS_COLS}
    for name, std in seed_bind.items():
        conn.execute("UPDATE meta_column SET compliance_id=? WHERE table_id=? AND col_name=?",
                     (std, tid, name))
    conn.commit()
    cols = conn.execute("SELECT compliance_id FROM meta_column WHERE table_id=?", (tid,)).fetchall()
    assert len(cols) == 12
    bound = [c for c in cols if c["compliance_id"]]
    assert len(bound) == 3
    conn.close()


def test_metrics_have_similar_pairs():
    """口径 diff 演示位：对公/零售日均余额只差 WHERE 条件。"""
    conn = sqlite3.connect(META_DB)
    conn.row_factory = sqlite3.Row
    a = conn.execute("SELECT calc_sql FROM metric_def WHERE name='对公客户日均余额'").fetchone()
    b = conn.execute("SELECT calc_sql FROM metric_def WHERE name='零售客户日均余额'").fetchone()
    assert a and b and "cust_type='2'" in a["calc_sql"] and "cust_type='1'" in b["calc_sql"]
    conn.close()


def test_business_db_tables():
    import duckdb
    conn = duckdb.connect(str(BUSINESS_DB), read_only=True)
    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    expect = {"dim_org", "dim_product", "dim_customer", "dwd_account", "dwd_trx",
              "dwd_cust_event", "dwm_cust_balance_daily", "ods_customer_src"}
    assert expect <= tables
    assert conn.execute("SELECT COUNT(*) FROM ods_customer_src").fetchone()[0] == 200
    conn.close()
