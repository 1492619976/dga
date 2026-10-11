"""CLI：dga chat（对话）/ dga mcp（工具面暴露为 MCP server）。

W12 实现 chat（rich TUI，SSE 客户端）；dga mcp 在 fastmcp 装好后即可用。
"""
from __future__ import annotations

import argparse


def main() -> None:
    p = argparse.ArgumentParser(prog="dga", description="DGA · Data Governance Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat", help="终端对话（W12）")
    sub.add_parser("mcp", help="把工具面暴露为 MCP server（stdio）")
    args = p.parse_args()

    if args.cmd == "mcp":
        from fastmcp import FastMCP

        mcp = FastMCP("dga-tools")
        from .tools import ingest_tools as it, govern_tools as gt, report_tools as rt
        for fn in (it.list_databases, it.list_tables, it.get_schema, it.profile_table,
                   gt.list_standards, gt.get_metric, rt.check_mapping):
            mcp.tool(fn)
        mcp.run()  # stdio
    elif args.cmd == "chat":
        raise SystemExit("dga chat lands in W12; 用 uvicorn dga.server:app --reload 先起 Web")


if __name__ == "__main__":
    main()
