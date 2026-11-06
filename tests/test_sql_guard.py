"""The gateway's SELECT-only check, on its own."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import server


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select 1;",
    "FROM read_csv('/data/a.csv')",
    "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
    "DESCRIBE SELECT 1",
    "SUMMARIZE SELECT 1",
    "SHOW TABLES",
    "PRAGMA show_tables",
    "SELECT 1; SELECT 2",
    "-- comment\nSELECT 1",
    "SELECT 'COPY x TO y' AS s",
])
def test_allowed(sql):
    server.check_sql(sql)


@pytest.mark.parametrize("sql", [
    "COPY (SELECT 1) TO '/data/x.csv'",
    "EXPORT DATABASE '/data/x'",
    "ATTACH '/data/a.sqlite' AS a (TYPE sqlite, READ_ONLY)",
    "DETACH a",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "CREATE TABLE t (a INT)",
    "CREATE TEMP VIEW v AS SELECT 1",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN b INT",
    "SET threads = 1",
    "RESET threads",
    "INSTALL httpfs",
    "LOAD httpfs",
    "CALL pragma_version()",
    "CHECKPOINT",
    "VACUUM",
    "EXPLAIN SELECT 1",
    "EXPLAIN ANALYZE COPY (SELECT 1) TO '/data/x.csv'",
    "BEGIN",
    "PREPARE p AS SELECT 1",
    "SELECT 1; DROP TABLE t",
])
def test_rejected(sql):
    with pytest.raises(ToolError, match="Only read-only SELECT"):
        server.check_sql(sql)


@pytest.mark.parametrize("sql", ["", "  ", ";;"])
def test_empty(sql):
    with pytest.raises(ToolError, match="No SQL statement"):
        server.check_sql(sql)


def test_parse_error():
    with pytest.raises(ToolError, match="Could not parse"):
        server.check_sql("SELEC 1")


def test_init_sql_order_and_quoting():
    sql = server.build_init_sql(Path("/da'ta"), "/opt/duckdb/extensions", "1GB")
    lines = sql.splitlines()
    assert lines[0] == "SET extension_directory = '/opt/duckdb/extensions';"
    assert "SET allowed_directories = ['/da''ta/'];" in lines
    assert "SET memory_limit = '1GB';" in lines
    # Loading happens before outside access is cut off, and the lock is last.
    assert lines.index("LOAD sqlite;") < lines.index("SET enable_external_access = false;")
    assert lines[-1] == "SET lock_configuration = true;"


def test_init_sql_trailing_slash_once():
    assert "['/data/']" in server.build_init_sql(Path("/data"), "/x")
