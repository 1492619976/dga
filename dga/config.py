"""全局配置：路径与模型档位（OpenAI 兼容端点）。

四档模型分离（学 open_deep_research）：入口强模型、工具回执摘要轻模型，
成本可量化。环境变量优先于默认值。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 路径 ----
REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "demo"
BUSINESS_DB = Path(os.environ.get("DGA_BUSINESS_DB", DEMO_DIR / "bank_retail.duckdb"))
META_DB = Path(os.environ.get("DGA_META_DB", DEMO_DIR / "meta.db"))
SPEC_DOCS_DIR = DEMO_DIR / "spec_docs"
CONFIGS_DIR = REPO_ROOT / "configs"

# ---- LLM（OpenAI 兼容端点；未配置时 CLI/工具单测均可离线跑） ----
LLM_BASE_URL = os.environ.get("DGA_LLM_BASE_URL", "")  # 如 https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY = os.environ.get("DGA_LLM_API_KEY", "")
MODEL_STRONG = os.environ.get("DGA_MODEL_STRONG", "")   # 入口/意图/澄清
MODEL_LIGHT = os.environ.get("DGA_MODEL_LIGHT", "")     # 工具回执摘要/描述补全草稿

# ---- 数据库连接（业务库只读探查用） ----
BUSINESS_DSN = os.environ.get("DGA_BUSINESS_DSN", f"duckdb:///{BUSINESS_DB}")
