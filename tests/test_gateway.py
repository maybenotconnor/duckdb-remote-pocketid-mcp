"""End to end: an MCP client talks to the gateway, which runs the real pinned
upstream mcp-server-motherduck as a child process."""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from conftest import gateway_env, patched_env, result_json

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client(duckdb_assets, sandbox):
    with patched_env(gateway_env(duckdb_assets, sandbox)):
        mcp = server.build_server()
    async with Client(mcp) as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def tight_client(duckdb_assets, sandbox):
    env = gateway_env(duckdb_assets, sandbox, MAX_ROWS=5, MAX_CHARS=2000, QUERY_TIMEOUT=1)
    with patched_env(env):
        mcp = server.build_server()
    async with Client(mcp) as c:
        yield c


async def query(client, sql: str) -> dict:
    return result_json(await client.call_tool("execute_query", {"sql": sql}))


async def rejected(client, sql: str) -> str:
    result = await client.call_tool("execute_query", {"sql": sql}, raise_on_error=False)
    assert result.is_error, f"expected an error for: {sql}"
    return result.content[0].text


def files_in(path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


# ---------------------------------------------------------------- what the client sees

async def test_tools_are_query_and_readme_only(client):
    names = {t.name for t in await client.list_tools()}
    assert names == {"execute_query", "data_readme"}


async def test_query_tool_is_marked_read_only(client):
    tool = next(t for t in await client.list_tools() if t.name == "execute_query")
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


async def test_instructions_include_data_readme(client, sandbox):
    assert "Sample data" in client.instructions
    assert str(sandbox["data"]) in client.instructions


async def test_instructions_in_classic_initialize_handshake(duckdb_assets, sandbox):
    """Claude.ai and most clients still use the initialize handshake."""
    with patched_env(gateway_env(duckdb_assets, sandbox)):
        mcp = server.build_server()
    async with Client(mcp, mode="legacy") as c:
        assert "Sample data" in c.initialize_result.instructions


async def test_readme_tool(client):
    text = (await client.call_tool("data_readme", {})).content[0].text
    assert text.startswith("# Sample data")


async def test_hidden_upstream_tools_cannot_be_called(client):
    result = await client.call_tool("list_tables", {}, raise_on_error=False)
    assert result.is_error


# ---------------------------------------------------------------- one query per file type

async def test_jsonl_plain_and_gzip_with_wildcard(client, sandbox):
    r = await query(client, f"""
        SELECT action, count(*) AS n
        FROM read_json_auto('{sandbox["data"]}/events/*.jsonl*')
        GROUP BY action ORDER BY action""")
    assert r["rows"] == [["export", 1], ["login", 3], ["search", 1]]


async def test_csv_plain_and_gzip_with_wildcard(client, sandbox):
    r = await query(client, f"""
        SELECT region, sum(units * price) AS revenue
        FROM read_csv('{sandbox["data"]}/sales/*.csv*')
        GROUP BY ALL ORDER BY region""")
    assert r["rows"] == [["east", 19.25], ["north", 35.0], ["south", 40.0]]


async def test_parquet_with_recursive_glob(client, sandbox):
    r = await query(client, f"""
        SELECT host, max(cpu) AS peak, count(*) AS n
        FROM read_parquet('{sandbox["data"]}/**/*.parquet')
        GROUP BY host ORDER BY host""")
    assert r["rows"] == [["web-1", 13.0, 3], ["web-2", 14.0, 3]]


async def test_sqlite(client, sandbox):
    db = f"{sandbox['data']}/app.sqlite"
    r = await query(client, f"""
        SELECT u.name, sum(o.total) AS spent
        FROM sqlite_scan('{db}', 'users') u
        JOIN sqlite_scan('{db}', 'orders') o ON o.user_id = u.id
        GROUP BY u.name ORDER BY u.name""")
    assert r["rows"] == [["ana", 42.5], ["cy", 99.0]]
    tables = await query(client, f"SELECT name FROM sqlite_scan('{db}', 'sqlite_master') WHERE type = 'table' ORDER BY name")
    assert tables["rows"] == [["orders"], ["users"]]


async def test_list_files_and_describe(client, sandbox):
    r = await query(client, f"SELECT count(*) FROM glob('{sandbox['data']}/**')")
    assert r["rows"][0][0] >= 7
    r = await query(client, f"DESCRIBE FROM read_parquet('{sandbox['data']}/metrics/part-0.parquet')")
    assert [row[0] for row in r["rows"]] == ["day", "host", "cpu"]


# ---------------------------------------------------------------- confined to the data folder

@pytest.mark.parametrize("path", ["/etc/passwd", "{base}/secret.txt", "{data}/../secret.txt", "{base}/data-other/x.csv"])
async def test_reading_outside_data_folder_fails(client, sandbox, path):
    path = path.format(base=sandbox["base"], data=sandbox["data"])
    text = await rejected(client, f"SELECT * FROM read_text('{path}')")
    assert "Permission Error" in text
    assert "top secret" not in text and "root:" not in text


async def test_etc_passwd_as_csv_fails(client):
    assert "Permission Error" in await rejected(client, "SELECT * FROM read_csv('/etc/passwd')")


async def test_child_gets_no_gateway_secrets(monkeypatch):
    monkeypatch.setenv("POCKETID_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "k")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "k")
    env = server.child_env()
    assert not {"POCKETID_CLIENT_SECRET", "JWT_SIGNING_KEY", "STORAGE_ENCRYPTION_KEY", "AWS_SECRET_ACCESS_KEY"} & env.keys()
    assert env["PATH"] == os.environ["PATH"]


# ---------------------------------------------------------------- read only

@pytest.mark.parametrize("sql", [
    "COPY (SELECT 1) TO '{data}/out.csv'",
    "COPY (SELECT 1) TO '{data}/out.parquet' (FORMAT parquet)",
    "EXPORT DATABASE '{data}/export'",
    "ATTACH '{data}/new.sqlite' AS n (TYPE sqlite)",
    "ATTACH '{data}/new.duckdb' AS n",
    "ATTACH '{data}/app.sqlite' AS a (TYPE sqlite); DELETE FROM a.users",
    "CREATE TABLE t AS SELECT 1",
    "CREATE TEMP TABLE t AS SELECT 1",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "SELECT 1; COPY (SELECT 1) TO '{data}/out.csv'",
    "EXPLAIN ANALYZE COPY (SELECT 1) TO '{data}/out.csv'",
    "/* SELECT */ COPY (SELECT 1) TO '{data}/out.csv'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access = true",
    "SET lock_configuration = false",
    "RESET allowed_directories",
    "CREATE SECRET s (TYPE s3, KEY_ID 'a', SECRET 'b')",
    "CHECKPOINT",
    "VACUUM",
])
async def test_writes_and_setting_changes_are_rejected(client, sandbox, sql):
    before = files_in(sandbox["data"])
    text = await rejected(client, sql.format(data=sandbox["data"]))
    assert "Only read-only SELECT queries are allowed" in text
    assert files_in(sandbox["data"]) == before


async def test_settings_stay_locked(client):
    r = await query(client, "SELECT value FROM duckdb_settings() WHERE name = 'lock_configuration'")
    assert r["rows"] == [["true"]]


async def test_unparseable_sql_is_rejected(client):
    assert "Could not parse SQL" in await rejected(client, "SELEC 1")


async def test_empty_sql_is_rejected(client):
    assert "No SQL statement" in await rejected(client, " ; ")


# ---------------------------------------------------------------- concurrent queries

async def test_concurrent_queries_all_succeed(client, sandbox):
    """Regression: overlapping queries shared one locked DuckDB instance, so
    every query after the first failed its init SQL."""
    d = sandbox["data"]
    sqls = [
        f"SELECT count(*) FROM read_csv('{d}/sales/*.csv*'), range(200000)",
        f"SELECT count(*) FROM read_json_auto('{d}/events/*.jsonl*'), range(200000)",
        f"SELECT count(*) FROM read_parquet('{d}/**/*.parquet'), range(200000)",
        f"SELECT count(*) FROM sqlite_scan('{d}/app.sqlite', 'users'), range(200000)",
    ] * 3
    results = await asyncio.gather(
        *[client.call_tool("execute_query", {"sql": sql}, raise_on_error=False) for sql in sqls]
    )
    errors = [r.content[0].text[:200] for r in results if r.is_error]
    assert errors == []
    assert [result_json(r)["rows"][0][0] for r in results] == [1_000_000, 1_000_000, 1_200_000, 600_000] * 3


async def test_concurrent_queries_keep_the_sandbox(client, sandbox):
    """Every concurrent query still runs on a locked, confined instance."""
    calls = [client.call_tool("execute_query", {"sql": "SELECT * FROM read_text('/etc/passwd')"}, raise_on_error=False)
             for _ in range(4)]
    calls += [client.call_tool("execute_query", {"sql": "SELECT value FROM duckdb_settings() WHERE name = 'lock_configuration'"})
              for _ in range(4)]
    results = await asyncio.gather(*calls)
    assert all(r.is_error and "Permission Error" in r.content[0].text for r in results[:4])
    assert all(result_json(r)["rows"] == [["true"]] for r in results[4:])


async def test_timeout_does_not_block_the_queue(tight_client):
    slow = tight_client.call_tool("execute_query", {"sql": "SELECT count(*) FROM range(1000000000000) a"}, raise_on_error=False)
    fast = [tight_client.call_tool("execute_query", {"sql": "SELECT 1"}) for _ in range(3)]
    results = await asyncio.gather(slow, *fast)
    assert "timed out" in results[0].content[0].text
    assert all(result_json(r)["rows"] == [[1]] for r in results[1:])


# ---------------------------------------------------------------- results stay small

async def test_max_rows(tight_client):
    r = await query(tight_client, "SELECT * FROM range(100)")
    assert r["rowCount"] == 5 and r["truncated"] is True


async def test_max_chars(tight_client):
    r = await query(tight_client, "SELECT repeat('x', 600) AS s FROM range(5)")
    assert r["truncated"] is True and r["rowCount"] < 5


async def test_query_timeout(tight_client):
    text = await rejected(tight_client, "SELECT count(*) FROM range(1000000000000) a")
    assert "timed out" in text


async def test_still_works_after_timeout(tight_client):
    assert (await query(tight_client, "SELECT 42"))["rows"] == [[42]]
