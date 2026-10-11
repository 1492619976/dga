"""SQL 守卫：所有对业务库执行的 SQL 必须先过 check()。

只看语句首 token 的动词（写 DML/DDL 一律拦截），不误杀 SELECT 里出现的列名/表名。
ATTACH/DETACH 是 dry_run_check 的白名单特例（临时对账库），见 allow_attach。
"""
from __future__ import annotations

import re

# 绝对禁止（任何工具、任何 plan 都不放行）
_BLOCKED_VERBS = {
    "DROP", "TRUNCATE", "DELETE", "ALTER", "GRANT", "REVOKE",
    "UPDATE", "INSERT", "MERGE", "REPLACE", "CALL", "EXECUTE",
    "CREATE", "SET", "COMMENT", "VACUUM", "LOCK", "ATTACH", "DETACH",
}

# 只读白名单（首动词）
_READONLY_VERBS = {
    "SELECT", "PRAGMA", "DESCRIBE", "DESC", "EXPLAIN", "SHOW", "SUMMARIZE",
}

_HEAD = re.compile(r"^\s*([A-Za-z]+)")


class SqlGuardError(PermissionError):
    """黑名单命中。message 会进入审计日志（status=blocked）。"""


def check(sql: str, *, allow_attach: bool = False) -> str:
    """校验并返回原 SQL；命中黑名单抛 SqlGuardError。

    幂等、零副作用、纯字符串操作——工具层在执行前调用，
    是提示词红线之外的第二道（也是最后一道）防线。
    """
    verb = _head_verb(sql)
    if verb == "WITH":
        return _check_cte(sql)
    if verb == "":
        raise SqlGuardError("empty or unparsable SQL")
    if verb in ("ATTACH", "DETACH") and allow_attach:
        return sql
    if verb in _READONLY_VERBS:
        return sql
    raise SqlGuardError(
        f"verb '{verb}' is not read-only whitelisted"
        + ("" if not allow_attach else " (ATTACH/DETACH allowed for dry-run only)"))


def _head_verb(sql: str) -> str:
    m = _HEAD.match(sql or "")
    return m.group(1).upper() if m else ""


def _check_cte(sql: str) -> str:
    """WITH 开头的语句：CTE 右括号之后的主动词必须还是 SELECT。"""
    depth, last_close = 0, -1
    for i, ch in enumerate(sql):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                last_close = i
    tail = sql[last_close + 1:].strip() if last_close >= 0 else ""
    verb = _head_verb(tail)
    if verb not in ("", "SELECT"):
        raise SqlGuardError(f"CTE main verb must be SELECT, got {verb}")
    return sql
