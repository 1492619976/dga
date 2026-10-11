# AGENTS.md — DGA 仓库指南（供 AI coding agent 与人类贡献者）

> 本项目欢迎 AI 辅助贡献，但必须遵守本章规范（参考 Apache flink-agents 的 AI 辅助 PR 实践）。

## 项目结构

```
dga/            Python 包
  server.py     FastAPI + SSE 对话入口
  graph/        编排层（LangGraph）：supervisor 路由 / gate 审批门 / agents 三子代理
  tools/        工具面（fastmcp，独立 MCP server）+ sql_guard 黑名单守卫
  meta/         SQLite 元数据访问层（7 张表）
  rag/          规范文档检索
  prompts/      系统提示词（即功能规格，修改须过测试）
demo/           自编演示数据：build_demo.py 一键重建业务库+元数据+规范文档
web/            极简对话前端（原生 JS 单页）
evals/          deepeval 评测集与运行器
tests/          pytest
```

分层铁律：`壳 → 编排 → 工具 → 数据` 单向依赖。工具层不得 import 编排层；工具必须可被 MCP 客户端独立调用。

## 硬规则（任何改动不得违反）

1. **sql_guard 是最后防线**：所有对业务库执行 SQL 的工具必须先过 `sql_guard.check()`；新增工具也不例外。
2. **写操作必须带 plan_id**：改元数据/存配置/存规则的工具，第一行校验 `gate.assert_valid_plan()`。
3. **提示词改动必须跑剧本测试**：`prompts/*.md` 改动后跑 `pytest tests/test_graph.py -m script`。
4. **demo 数据可一键重建**：改了 `demo/build_demo.py` 后执行它，确保零报错；demo 产物不入 git。
5. **脱敏**：不得引入任何真实机构的数据、表名、监管制度原文；PII 样例值在工具输出一律脱敏。

## AI 辅助 PR 规范

- PR 描述中回答模板里的 generative-AI 问题；若实现主要由 AI 辅助完成，commit message 与 PR 描述均加一行：
  `Generated-by: <工具名 版本> (<模型名 版本>)`
- AI 辅助的实质改动须在 PR 中说明：运行时流程 / 关键决策 / 行为契约 / 失败行为 / 兼容性影响。
- 纯文档/注释改动不适用。

## 构建与测试

```bash
pip install -e ".[dev]"
python demo/build_demo.py     # 重建 demo 数据
pytest                        # 单测（不依赖 LLM）
python evals/run_eval.py      # 评测（依赖 LLM 端点，W14 起用）
```
