"""duckdb-remote-pocketid-mcp gateway.

Starts the pinned upstream `mcp-server-motherduck` as a stdio child process,
locked to read-only SQL over one data folder, and serves it over streamable
HTTP at /mcp behind a PocketID (OIDC) OAuth proxy.

Read-only is enforced in three independent layers:

1. Kernel: the data folder is a read-only bind mount and the container root
   filesystem is read-only (compose.yaml).
2. Gateway: `execute_query` accepts only SELECT statements, classified by
   DuckDB's own parser (`check_sql` below).
3. DuckDB: an empty read-only main database, plus `--init-sql` that limits
   file access to the data folder, turns off extension install/autoload and
   locks the configuration (`build_init_sql` below).

Everything is set by environment variables. See .env.example and README.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import duckdb
from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

log = logging.getLogger("duckdb-remote-pocketid-mcp")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")

NAME = "duckdb-remote-pocketid-mcp"

# The only upstream tool exposed. Upstream's list_databases / list_tables /
# list_columns describe the empty main database, so they would only mislead.
QUERY_TOOL = "execute_query"
README_TOOL = "data_readme"

# Statement types execute_query accepts. DESCRIBE, SHOW, SUMMARIZE, PRAGMA
# show_tables, FROM-first queries and WITH ... SELECT all parse as SELECT.
# EXPLAIN is excluded on purpose: EXPLAIN ANALYZE runs its inner statement,
# which could be a COPY.
ALLOWED_STATEMENTS = frozenset({"SELECT"})

# Redirect URIs OAuth clients may register: Claude's callbacks, and loopback on
# any port and path (FastMCP's rule for a loopback pattern with no port) for
# Claude Code and other local clients.
DEFAULT_REDIRECT_URIS = ",".join([
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
])

# The child gets only these variables. Gateway secrets never reach it.
CHILD_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE")


# --------------------------------------------------------------------------- config

def env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Read NAME, or the file named by NAME_FILE (Docker secrets)."""
    value = os.environ.get(name)
    if value is None and (path := os.environ.get(f"{name}_FILE")):
        value = Path(path).read_text().strip()
    if value is None or value == "":
        value = default
    if required and not value:
        sys.exit(f"{NAME}: {name} (or {name}_FILE) must be set")
    return value


def integer(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)  # type: ignore[arg-type]
    except ValueError:
        sys.exit(f"{NAME}: {name} must be an integer, not {raw!r}")


def csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in (env(name, default) or "").split(",") if item.strip()]


def data_dir() -> Path:
    path = Path(env("DATA_DIR", "/data") or "/data")
    if not path.is_absolute():
        sys.exit(f"{NAME}: DATA_DIR must be an absolute path, not {str(path)!r}")
    # resolve() so the allowed prefix matches the paths DuckDB checks.
    return path.resolve()


# --------------------------------------------------------------------------- DuckDB sandbox

def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_init_sql(data: Path, extension_dir: str, memory_limit: str | None = None) -> str:
    """SQL that upstream runs on every new connection, before each query.

    Order matters: extensions load and settings change before the lock.
    Setting names checked against DuckDB 1.5.6 (scripts/prepare_duckdb.py
    fails the image build if any is missing).
    """
    # Trailing slash, so /data does not also allow /data-other.
    prefix = str(data).rstrip("/") + "/"
    lines = [
        f"SET extension_directory = {sql_string(extension_dir)};",
        "LOAD sqlite;",  # installed at image build; json and parquet are built in
        "SET autoinstall_known_extensions = false;",
        "SET autoload_known_extensions = false;",
        "SET allow_community_extensions = false;",
        "SET allow_persistent_secrets = false;",
        # DuckDB adds the spill folder (default '<db>.tmp') to allowed_directories,
        # which would make it readable and writable. No spill folder, no extra path.
        "SET temp_directory = '';",
    ]
    if memory_limit:
        lines.append(f"SET memory_limit = {sql_string(memory_limit)};")
    lines += [
        f"SET allowed_directories = [{sql_string(prefix)}];",
        "SET enable_external_access = false;",
        "SET lock_configuration = true;",
    ]
    return "\n".join(lines)


def check_sql(sql: str) -> None:
    """Raise ToolError unless every statement in SQL is a SELECT."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise ToolError(f"Could not parse SQL: {e}") from None
    if not statements:
        raise ToolError("No SQL statement found.")
    for statement in statements:
        kind = statement.type.name
        if kind not in ALLOWED_STATEMENTS:
            raise ToolError(
                f"Only read-only SELECT queries are allowed; got a {kind} statement. "
                "This server cannot write, attach, copy, export, load or change settings."
            )


class ReadOnlyGuard(Middleware):
    """Expose only the query and README tools, check SQL before upstream sees
    it, and send upstream one query at a time.

    Why one at a time: upstream opens a fresh read-only connection per query
    and runs the init SQL on it. DuckDB's Python client hands a connection to
    a file that is already open the *same* database instance, and that
    instance is already locked, so the second query's init SQL fails ("the
    configuration has been locked"). Only when every connection has closed
    does the next connection get a fresh instance. Serialising queries keeps
    that true. Queued queries wait at most QUERY_TIMEOUT each.
    """

    def __init__(self, visible: set[str]):
        self.visible = visible
        self._query_lock = asyncio.Lock()

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if tool.name in self.visible]

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        if name not in self.visible:
            raise ToolError(f"Unknown tool: {name}")
        if name != QUERY_TOOL:
            return await call_next(context)
        sql = (context.message.arguments or {}).get("sql")
        if not isinstance(sql, str):
            raise ToolError("execute_query needs a 'sql' string.")
        check_sql(sql)
        async with self._query_lock:
            return await call_next(context)


# --------------------------------------------------------------------------- README / instructions

def read_data_readme(data: Path, max_chars: int) -> str | None:
    path = data / "README.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[README.md truncated at {max_chars} characters]"
    return text


def build_instructions(data: Path, readme: str | None, max_rows: int, max_chars: int, timeout: int) -> str:
    d = str(data)
    timeout_text = f"{timeout} seconds" if timeout > 0 else "no limit"
    text = f"""\
Read-only SQL over the files in {d}, using DuckDB {duckdb.__version__}.

Rules:
- Use the `{QUERY_TOOL}` tool. Only SELECT queries run (also FROM-first queries,
  WITH, DESCRIBE, SUMMARIZE, SHOW). Writes, COPY, EXPORT, ATTACH, INSTALL, LOAD
  and SET are rejected.
- Every path must be absolute and under {d}/. Nothing outside it is readable.
- Each call runs on a fresh connection. Nothing persists between calls, so put
  everything one answer needs in a single query (CTEs are fine).
- Results stop at {max_rows} rows or {max_chars} characters. Queries stop after {timeout_text}.
  Aggregate, filter and use LIMIT.

Reading files (gzip is detected from the .gz suffix; globs like * and ** work):
- List files:  FROM glob('{d}/**') LIMIT 200
- JSON lines:  FROM read_json_auto('{d}/logs/*.jsonl.gz')
- CSV:         FROM read_csv('{d}/exports/*.csv', union_by_name = true)
- Parquet:     FROM read_parquet('{d}/**/*.parquet')
- Plain text:  FROM read_text('{d}/notes/*.md')
- SQLite:      FROM sqlite_scan('{d}/app.sqlite', 'users')
  SQLite tables: FROM sqlite_scan('{d}/app.sqlite', 'sqlite_master')
- Add filename = true to read_csv / read_json_auto / read_parquet to see which file a row came from.
- Check a file's columns first: DESCRIBE FROM read_parquet('{d}/x.parquet')
"""
    if readme:
        text += f"\n## About this data (from {d}/README.md)\n\n{readme}\n"
    else:
        text += f"\nThere is no {d}/README.md describing the data. Start by listing files with glob().\n"
    return text


# --------------------------------------------------------------------------- auth

def build_auth():
    mode = (env("MCP_AUTH", "oidc") or "oidc").lower()
    if mode == "none":
        log.warning("MCP_AUTH=none: the server has NO authentication. Use this for local tests only.")
        return None
    if mode != "oidc":
        sys.exit(f"{NAME}: MCP_AUTH must be 'oidc' or 'none', not {mode!r}")

    from cryptography.fernet import Fernet
    from fastmcp.server.auth.oidc_proxy import OIDCProxy
    from key_value.aio.stores.filetree import FileTreeStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    # Read every required setting before touching disk or the network.
    issuer = (env("POCKETID_ISSUER_URL", required=True) or "").rstrip("/")
    client_id = env("POCKETID_CLIENT_ID", required=True)
    client_secret = env("POCKETID_CLIENT_SECRET", required=True)
    base_url = env("MCP_BASE_URL", required=True)
    jwt_key = env("JWT_SIGNING_KEY", required=True)
    storage_key = env("STORAGE_ENCRYPTION_KEY", required=True)

    # Client registrations and upstream tokens survive restarts, encrypted at rest.
    storage = FernetEncryptionWrapper(
        FileTreeStore(data_directory=env("OAUTH_STORAGE_DIR", "/state/oauth")),
        fernet=Fernet(storage_key),
    )

    # Same PocketID settings as opencloud-pocketid-mcp and
    # himalaya-remote-pocketid-mcp. PocketID itself refuses sign-in to users
    # outside the client's "Allowed user groups".
    return OIDCProxy(
        config_url=f"{issuer}/.well-known/openid-configuration",
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        required_scopes=csv("POCKETID_SCOPES", "openid,profile,email"),
        verify_id_token=True,
        # Clients may register only these redirect URIs, so an authorization
        # code can only ever go to Claude or to the user's own machine.
        allowed_client_redirect_uris=csv("ALLOWED_CLIENT_REDIRECT_URIS", DEFAULT_REDIRECT_URIS),
        # PocketID 2.14+ rejects an RFC 8707 `resource` that is not a registered
        # API. FastMCP still checks the client's `resource` against this server.
        forward_resource=False,
        client_storage=storage,
        jwt_signing_key=jwt_key,
    )


# --------------------------------------------------------------------------- upstream child

def upstream_command() -> str:
    """The upstream console script, preferring the one next to this Python."""
    configured = env("UPSTREAM_BIN")
    if configured:
        return configured
    sibling = Path(sys.executable).parent / "mcp-server-motherduck"
    return str(sibling) if sibling.exists() else "mcp-server-motherduck"


def upstream_args(data: Path) -> list[str]:
    db_path = env("DUCKDB_DB_PATH", "/opt/duckdb/empty.duckdb") or ""
    if not Path(db_path).is_file():
        sys.exit(f"{NAME}: DUCKDB_DB_PATH {db_path!r} does not exist (scripts/prepare_duckdb.py creates it)")
    init_sql = build_init_sql(
        data,
        extension_dir=env("DUCKDB_EXTENSION_DIR", "/opt/duckdb/extensions") or "",
        memory_limit=env("DUCKDB_MEMORY_LIMIT"),
    )
    # No --read-write: upstream opens the database read-only, with a fresh
    # connection (and a fresh run of init SQL) for every query.
    # No --allow-switch-databases: the database cannot be changed at runtime.
    return [
        "--transport", "stdio",
        "--db-path", db_path,
        "--max-rows", str(integer("MAX_ROWS", 1000)),
        "--max-chars", str(integer("MAX_CHARS", 50000)),
        "--query-timeout", str(integer("QUERY_TIMEOUT", 30)),
        "--init-sql", init_sql,
    ]


def child_env() -> dict[str, str]:
    kept = {k: v for k, v in os.environ.items() if k in CHILD_ENV_KEEP}
    # DuckDB looks under HOME for secrets and extensions; give it nothing.
    kept["HOME"] = "/nonexistent"
    # Upstream logs a full traceback for every failed query (a typo in SQL is
    # enough). Keep the logs readable; set UPSTREAM_LOG_LEVEL=INFO to debug.
    kept["FASTMCP_LOG_LEVEL"] = env("UPSTREAM_LOG_LEVEL", "CRITICAL") or "CRITICAL"
    kept["FASTMCP_SHOW_SERVER_BANNER"] = "false"
    kept["FASTMCP_ENABLE_RICH_TRACEBACKS"] = "false"
    kept["FASTMCP_ENABLE_RICH_LOGGING"] = "false"
    return kept


# --------------------------------------------------------------------------- server

def build_server() -> FastMCP:
    data = data_dir()
    if not data.is_dir():
        sys.exit(f"{NAME}: DATA_DIR {str(data)!r} is not a directory. Mount the data folder there.")

    max_rows = integer("MAX_ROWS", 1000)
    max_chars = integer("MAX_CHARS", 50000)
    timeout = integer("QUERY_TIMEOUT", 30)
    readme_max = integer("README_MAX_CHARS", 20000)

    readme = read_data_readme(data, readme_max)
    log.info("data folder %s; README.md %s", data, "found" if readme else "not found")

    mcp = FastMCP(
        name=env("MCP_SERVER_NAME", "duckdb") or "duckdb",
        instructions=build_instructions(data, readme, max_rows, max_chars, timeout),
        auth=build_auth(),
    )

    transport = StdioTransport(
        command=upstream_command(),
        args=upstream_args(data),
        env=child_env(),
        cwd="/",
        keep_alive=True,  # one upstream process, reused across requests
    )
    mcp.mount(create_proxy(transport, name="mcp-server-motherduck"))

    @mcp.tool(
        name=README_TOOL,
        description=(
            f"Return {data}/README.md, the operator's description of the data folder: "
            "what the files are, their layout and their columns. Read it before your first query."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    )
    def data_readme() -> str:
        text = read_data_readme(data, readme_max)
        return text or f"There is no README.md in {data}. List files with: FROM glob('{data}/**') LIMIT 200"

    mcp.add_middleware(ReadOnlyGuard({QUERY_TOOL, README_TOOL}))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


if __name__ == "__main__":
    build_server().run(
        transport="http",
        host=env("HOST", "0.0.0.0"),
        port=integer("PORT", 8000),
        path=env("MCP_PATH", "/mcp"),
        show_banner=False,
    )
