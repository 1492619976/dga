"""治理工具（14 个，四技能包）。W12-W13 实现；本文件先落接口签名与规格。

元数据：enrich_description / classify_pii / classify_level
标准：  list_standards / recommend_standards / check_compliance / bind_compliance(写)
质量：  gen_quality_rules / run_quality_rules / root_cause / retire_rules
指标：  query_metric / diff_metrics / lineage / register_metric(写)

查询类已可直连元数据库（见 meta.store DAO）；其余 TODO 标注实现要点。
"""
from __future__ import annotations

from typing import Any

from ..config import META_DB
from ..meta import store


def _c():
    return store.connect(META_DB)


def list_standards(domain: str | None = None) -> list[dict[str, Any]]:
    """查标准库（id/名称/定义/类型/码值/命名规则）。"""
    with _c() as conn:
        return store.list_standards(conn, domain)


def get_metric(name: str) -> dict[str, Any] | None:
    """查指标定义（业务口径/SQL/负责人/频率/粒度）。SQL 白话翻译由 Agent 层做。"""
    with _c() as conn:
        return store.get_metric(conn, name)


# ---- TODO W12/W13（实现要点见各 docstring） ----
def recommend_standards(table: str):  # pragma: no cover
    """贯标推荐：字段名相似度×0.4 + 类型兼容×0.3 + 码值 Jaccard×0.3，输出置信度与理由。"""
    raise NotImplementedError


def check_compliance(table: str):  # pragma: no cover
    """落标三层检查：①类型兼容 ②命名规则 ③distinct vs 标准码值 → 贯标率+建议。"""
    raise NotImplementedError


def diff_metrics(metric_a: str, metric_b: str):  # pragma: no cover
    """招牌工具：sqlglot 解析两指标 SQL，四类 diff（表/过滤/粒度/JOIN）逐类输出。"""
    raise NotImplementedError


def lineage(obj: str, direction: str = "downstream", depth: int = 2):  # pragma: no cover
    """解析 etl_sql 全集建血缘图（mermaid）+ 影响分析（下游表/指标清单）。"""
    raise NotImplementedError


def gen_quality_rules(table: str, business_def: str | None = None):  # pragma: no cover
    """六性模板生成规则（完整性/唯一/有效/一致/及时/准确）；business_def 非空走 LLM Proposer。"""
    raise NotImplementedError
