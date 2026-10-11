"""报送工具（5 个）。W13 实现；RAG v0.1 用 BM25 关键词检索顶，v0.2 换 Chroma+bge-m3。

ask_spec / check_mapping / gen_check_rules / preview_report / gen_report_sql
铁律：回答带出处（文档名+章节）；报送 SQL 永远只生成不执行。
"""
from __future__ import annotations

from ..config import META_DB
from ..meta import store


def check_mapping(report_table: str):
    """映射三类检查骨架已可跑数据侧：缺失映射/类型不匹配/码值不一致（TODO W13 完成码值层）。"""
    with store.connect(META_DB) as conn:
        return store.list_report_mapping(conn, report_table)


# TODO W13: ask_spec(BM25) / gen_check_rules / preview_report / gen_report_sql
