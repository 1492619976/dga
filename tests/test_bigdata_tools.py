"""bigdata 工具组（demo 模式）单测。"""
import pytest

from dga.config import META_DB
from dga.tools import bigdata_tools as bd


@pytest.fixture(scope="module", autouse=True)
def env():
    bd.init_bigdata_env()
    yield


def test_kafka_roundtrip():
    before = {t["messages"] for t in bd.kafka_topics() if t["topic"] == "dga.dq.alerts"}
    r = bd.kafka_produce("dga.dq.alerts", "test-key", '{"rule":"t"}')
    assert r["produced"]
    msgs = bd.kafka_consume("dga.dq.alerts")
    assert any(m["key"] == "test-key" for m in msgs)
    assert len(before) == 0 or r["topic_size"] > list(before)[0]


def test_hdfs_ls_cat_put():
    bd.hdfs_put("/user/dga/tmp/t1.txt", "hello dga")
    assert any(r["path"].endswith("t1.txt") for r in bd.hdfs_ls("/user/dga/tmp"))
    assert "hello dga" in bd.hdfs_cat("/user/dga/tmp/t1.txt")


def test_hdfs_path_escape_blocked():
    with pytest.raises(PermissionError):
        bd.hdfs_cat("../../etc/passwd")


def test_zk_set_get():
    bd.zk_set("/dga/test", "v1")
    assert bd.zk_get("/dga/test")["value"] == "v1"


def test_flink_submit_dag():
    r = bd.flink_sql_submit(
        "INSERT INTO dm_channel_rt SELECT channel, COUNT(*) cnt FROM dwd_trx "
        "WHERE trx_date = CURRENT_DATE GROUP BY channel", name="t-job")
    assert r["status"].startswith("RUNNING")
    assert "dwd_trx" in r["plan"]["sources"]
    assert "dm_channel_rt" in r["plan"]["sinks"]
    assert any("过滤" in op or "分组" in op for op in r["plan"]["operators"])
    assert any(j["name"] == "t-job" for j in bd.flink_jobs())


def test_audit_written():
    import sqlite3
    conn = sqlite3.connect(META_DB)
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE tool='kafka_produce'").fetchone()[0]
    assert n >= 1
    conn.close()
