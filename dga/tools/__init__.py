"""工具面：25 个工具 = 独立 MCP server（fastmcp）。

设计：工具层不知道编排层的存在——`dga mcp` 子命令把本包直接暴露给
ZCode/Claude Code 等 MCP 客户端使用。
"""
