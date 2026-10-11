"""sql_guard 单测（方案 TC-T03/TC-T04）。"""
import pytest

from dga.tools.sql_guard import SqlGuardError, check


def test_select_passes():
    assert check("SELECT * FROM customer WHERE id = 1") == "SELECT * FROM customer WHERE id = 1"


def test_delete_blocked():
    with pytest.raises(SqlGuardError):
        check("DELETE FROM customer")


def test_drop_blocked():
    with pytest.raises(SqlGuardError):
        check("DROP TABLE customer")


def test_no_false_positive_on_column_named_delete():
    check("SELECT is_deleted, delete_flag FROM updates_log")  # 列名含 delete 不得误杀


def test_update_blocked():
    with pytest.raises(SqlGuardError):
        check("UPDATE customer SET name = 'x'")


def test_cte_select_passes():
    check("WITH t AS (SELECT 1 AS a) SELECT a FROM t")


def test_cte_insert_blocked():
    with pytest.raises(SqlGuardError):
        check("WITH t AS (SELECT 1 AS a) INSERT INTO x SELECT a FROM t")


def test_attach_blocked_by_default():
    with pytest.raises(SqlGuardError):
        check("ATTACH ':memory:' AS tmp")


def test_attach_allowed_for_dry_run():
    check("ATTACH ':memory:' AS tmp", allow_attach=True)


def test_empty_sql():
    with pytest.raises(SqlGuardError):
        check("   ")
