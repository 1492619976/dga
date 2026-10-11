"""接入工具（只读探查）单测：demo 库上真跑。"""
import pytest

duckdb = pytest.importorskip("duckdb")

from dga.config import BUSINESS_DB  # noqa: E402
from dga.tools.ingest_tools import get_schema, list_tables, profile_table  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def built():
    if not BUSINESS_DB.exists():
        import demo.build_demo as bd
        bd.build_business_db()
    yield


def test_list_tables():
    tables = list_tables()
    names = {t["table"] for t in tables}
    assert "ods_customer_src" in names and "dwd_trx" in names
    assert all(t["rows"] > 0 for t in tables)


def test_get_schema_shape():
    s = get_schema("ods_customer_src")
    assert s["columns"][0]["name"] == "Cust_No" and len(s["columns"]) == 12


def test_profile_pii_and_dirty_values():
    p = profile_table("ods_customer_src")
    assert p["rows"] == 200
    cols = {c["col"]: c for c in p["columns"]}
    assert cols["CertNo"]["suspected_pii"] is True      # 证件号 PII
    assert "9" in cols["CERTYPE"]["top5"]               # 脏码值被画像捕获（码值层落标检查素材）
