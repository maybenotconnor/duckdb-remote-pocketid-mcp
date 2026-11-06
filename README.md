# duckdb-remote-pocketid-mcp

A remote MCP server that lets an AI client answer questions with read-only SQL
over a folder of files. DuckDB does the querying. PocketID protects it with OIDC.

```
AI client ──HTTPS──▶ reverse proxy ──▶ duckdb-remote-pocketid-mcp container
                                         ├─ FastMCP gateway (PocketID OAuth, /mcp, /health)
                                         │    └─ SELECT-only check on every query
                                         └─ mcp-server-motherduck (pinned, stdio child)
                                              └─ DuckDB, locked to /data (read-only mount)
```

It does not fork the upstream server. It installs
[`mcp-server-motherduck`](https://pypi.org/project/mcp-server-motherduck/) from
PyPI, pinned to one version, and runs it behind a gateway, the same pattern as
[himalaya-remote-pocketid-mcp](https://github.com/maybenotconnor/himalaya-remote-pocketid-mcp).

The client can read JSON lines, CSV, Parquet and SQLite files, gzipped or not,
with wildcards across many files. It cannot write anything or read anything
outside the data folder.

| File | What it holds |
|------|---------------|
| `server.py` | The gateway. |
| `requirements.txt` | The pinned upstream version. Change one line to update. |
| `compose.yaml` | The container, its read-only data mount and hardening. |
| `.env.example` | Public URL, PocketID client, data path, bind address, limits. |
| `secrets/` | PocketID client secret, JWT key, storage key. See `secrets/README.md`. |
| `scripts/prepare_duckdb.py` | Runs at image build: installs extensions, checks the DuckDB settings exist. |
| `tests/` | Sample data and tests. |

## Install

You need Docker with Compose, a PocketID instance, and a reverse proxy with HTTPS.

### 1. Create the PocketID client

In PocketID, go to **Admin > Settings > OIDC Clients** and create a client:

- Name: `duckdb-remote-pocketid-mcp`
- Type: **confidential** (Public client off)
- Callback URL: `https://<your MCP_BASE_URL host>/auth/callback`
- PKCE: on, with the S256 method
- Scopes: `openid profile email`
- **Allowed user groups**: the group whose members may use this server.
  PocketID refuses sign-in to everyone else.

Keep the client ID and client secret for the next step.

### 2. Get the code, secrets and settings

```sh
git clone https://github.com/maybenotconnor/duckdb-remote-pocketid-mcp.git
cd duckdb-remote-pocketid-mcp
scripts/make-secrets.sh           # creates secrets/jwt_signing_key and secrets/storage_encryption_key
```

Put the PocketID client secret in `secrets/pocketid_client_secret`. Then give the
secret files to the container user (UID 10001). Compose mounts file secrets with
their host owner and mode, so the container can't read files that belong to your
user with mode 600:

```sh
sudo chown 10001:10001 secrets/pocketid_client_secret secrets/jwt_signing_key secrets/storage_encryption_key
sudo chmod 400 secrets/pocketid_client_secret secrets/jwt_signing_key secrets/storage_encryption_key
```

Copy and edit the settings:

```sh
cp .env.example .env
```

Set at least `MCP_BASE_URL`, `POCKETID_ISSUER_URL`, `POCKETID_CLIENT_ID` and
`DATA_PATH` (the host folder to query). The other values have defaults.

### 3. Start

```sh
docker compose up -d --build
docker compose logs -f duckdb-remote-pocketid-mcp
curl http://127.0.0.1:8766/health        # prints: ok
```

The container needs outbound HTTPS to PocketID only. DuckDB makes no network
calls at runtime.

### 4. Publish through the reverse proxy

Point an HTTPS host for `MCP_BASE_URL` at `BIND_ADDR:HOST_PORT` (default
`127.0.0.1:8766`). Use `/health` as the health check. For example, with Caddy:

```
duckdb-mcp.example.com {
    reverse_proxy 127.0.0.1:8766
}
```

**Turn off any proxy-level login on this host** (Pangolin, Authelia, Cloudflare
Access and so on). AI clients connect from their providers' servers and can't
complete a browser login at the proxy. The PocketID OAuth in the container
controls access instead.

### 5. Connect a client

In Claude: **Customize > Connectors > Add custom connector**.

- URL: `https://<your MCP_BASE_URL host>/mcp`
- OAuth client: "Use Claude's published identity" or "Register automatically"

Claude opens a PocketID sign-in, then a consent page from the gateway. Approve both.
Other MCP clients that support OAuth work the same way with the same URL.

## Describe your data

Put a `README.md` at the top of the data folder. Say what the files are, how they
are laid out, and what their columns mean. The server sends it to the client as
part of its **server instructions**, which the client reads when it connects,
before its first query. It is also available as the `data_readme` tool, for
clients that ignore server instructions. Restart the container after you change
it so new connections get the new instructions (the tool always reads the
current file). `README_MAX_CHARS` caps its length (default 20,000).

The client sees two tools:

| Tool | What it does |
|------|--------------|
| `execute_query` | Runs one or more SELECT statements and returns rows as JSON. |
| `data_readme` | Returns the data folder's `README.md`. |

Example queries the client can run:

```sql
FROM glob('/data/**') LIMIT 200;                                          -- list files
SELECT action, count(*) FROM read_json_auto('/data/events/*.jsonl.gz') GROUP BY ALL;
FROM read_csv('/data/exports/*.csv', union_by_name = true, filename = true);
SELECT host, avg(cpu) FROM read_parquet('/data/**/*.parquet') GROUP BY host;
FROM sqlite_scan('/data/app.sqlite', 'users');
FROM sqlite_scan('/data/app.sqlite', 'sqlite_master');                    -- SQLite tables
```

## How read-only and confinement work

Three independent layers. Each blocks writes on its own.

1. **Kernel.** The data folder is mounted `:ro`, the container root filesystem is
   read-only (only `/tmp` is writable, as tmpfs), the process runs as UID 10001
   with no capabilities and `no-new-privileges`.
2. **Gateway.** `execute_query` runs only if every statement is a `SELECT`, as
   classified by DuckDB's own parser (`duckdb.extract_statements`). `FROM`-first
   queries, `WITH`, `DESCRIBE`, `SUMMARIZE` and `SHOW` all count as SELECT.
   `COPY`, `EXPORT`, `ATTACH`, `INSERT`, `CREATE`, `INSTALL`, `LOAD`, `SET`,
   `CALL` and `EXPLAIN` (whose `ANALYZE` form runs its inner statement) are
   rejected before upstream sees them.
3. **DuckDB.** Upstream opens an empty database file read-only, with a fresh
   connection per query. Before each query it runs this `--init-sql`:

   ```sql
   SET extension_directory = '/opt/duckdb/extensions';
   LOAD sqlite;                                 -- installed at image build
   SET autoinstall_known_extensions = false;
   SET autoload_known_extensions = false;
   SET allow_community_extensions = false;
   SET allow_persistent_secrets = false;
   SET temp_directory = '';                     -- see note below
   SET allowed_directories = ['/data/'];
   SET enable_external_access = false;
   SET lock_configuration = true;
   ```

   With external access off, DuckDB reads only paths under `/data/`; `/etc/passwd`,
   `/run/secrets/…`, `../` escapes, symlinks out of the folder and every URL fail.
   With the configuration locked, no query can turn any of this back on.

The setting names were checked against DuckDB 1.5.6, the version upstream 1.1.0
pins. `scripts/prepare_duckdb.py` fails the image build if any of them is missing
from the DuckDB version you build with.

Why three layers: in DuckDB, `allowed_directories` grants **write** access as
well as read access, so DuckDB on its own lets `COPY ... TO '/data/x.csv'`
through. `tests/test_sandbox.py::test_duckdb_alone_allows_copy_into_data_folder`
records this. The SELECT-only check and the read-only mount each stop it.

The gateway passes the child process only `PATH`, locale and timezone variables,
so the PocketID secret, JWT key and storage key never reach DuckDB.

**Notes**

- **SQLite uses `sqlite_scan`, not `ATTACH`.** DuckDB accepts `ATTACH` on a SQLite
  file that doesn't exist yet, and the gateway can't check whether an `ATTACH`
  says `READ_ONLY`, so `ATTACH` is rejected. `sqlite_scan` opens files read-only
  and fails on missing files.
- **Nothing persists between calls.** Each query gets a fresh connection, so the
  client puts everything one answer needs into a single query.
- **No spill to disk.** DuckDB adds its spill folder to `allowed_directories`, so
  spilling is off (`temp_directory = ''`). A query larger than memory fails with
  an out-of-memory error instead of writing temp files. Set `DUCKDB_MEMORY_LIMIT`
  and `MEM_LIMIT` to suit the host.

## Configuration

Set in `.env`. Compose passes them to the container.

| Variable | Default | What it does |
|----------|---------|--------------|
| `MCP_BASE_URL` | (required) | Public URL, no trailing slash. |
| `POCKETID_ISSUER_URL` | (required) | PocketID URL, e.g. `https://id.example.com`. |
| `POCKETID_CLIENT_ID` | (required) | OIDC client ID. The secret comes from `secrets/`. |
| `DATA_PATH` | (required) | Host folder to query. Mounted read-only. |
| `DATA_DIR` | `/data` | Where the data folder appears in the container and in queries. |
| `BIND_ADDR` / `HOST_PORT` | `127.0.0.1` / `8766` | Host address and port the proxy reaches. |
| `PORT` | `8000` | Port inside the container. |
| `MAX_ROWS` | `1000` | Rows returned per query. |
| `MAX_CHARS` | `50000` | Characters returned per query. |
| `QUERY_TIMEOUT` | `30` | Seconds before a query is stopped. |
| `DUCKDB_MEMORY_LIMIT` | DuckDB default | e.g. `1GB`. |
| `MEM_LIMIT` | `2g` | Container memory limit. |
| `README_MAX_CHARS` | `20000` | Longest data `README.md` sent to the client. |
| `UPSTREAM_LOG_LEVEL` | `CRITICAL` | Upstream's FastMCP log level. `INFO` to debug. |
| `LOG_LEVEL` | `INFO` | Gateway log level. |
| `ALLOWED_CLIENT_REDIRECT_URIS` | Claude + loopback | Redirect URIs OAuth clients may register, comma-separated. See [OAuth security](#oauth-security). |

Also read by the gateway, not needed with Compose: `HOST`, `MCP_PATH` (`/mcp`),
`POCKETID_SCOPES`, `OAUTH_STORAGE_DIR`, and
`MCP_AUTH=none` for local tests without OAuth (never expose that).

## Operation

| Task | Command |
|------|---------|
| Logs | `docker compose logs -f duckdb-remote-pocketid-mcp` |
| Health | `curl http://127.0.0.1:8766/health` |
| Restart after editing the data README | `docker compose restart` |
| Update upstream | Change the `mcp-server-motherduck==` line in `requirements.txt`, then `docker compose up -d --build` |

The `oauth-state` volume holds the gateway's encrypted OAuth client store.
Without it, clients must connect again.

### OAuth security

- **Redirect URI allowlist.** OAuth clients may register only the redirect URIs
  in `ALLOWED_CLIENT_REDIRECT_URIS`. The default is Claude's two callbacks
  (`https://claude.ai/api/mcp/auth_callback`,
  `https://claude.com/api/mcp/auth_callback`) and loopback on any port
  (`http://localhost`, `http://127.0.0.1`, `http://[::1]`) for Claude Code.
  An authorization code can only go to one of these.
- **Consent is bound to the browser.** FastMCP 4.1.6 ties the consent form to a
  cookie in the browser that opened it, and the PocketID callback to a cookie set
  on approval. A sign-in link forwarded to someone else fails with HTTP 403.
  FastMCP before 3.2.0 lacked the first check (GHSA-rww4-4w9c-7733), so
  `requirements.txt` requires 4.1.6 or later.
- **PocketID v2.** The gateway does not forward the RFC 8707 `resource`
  parameter to PocketID, which rejects it unless it names a registered API. The
  PocketID callback URL must be the exact `https://<host>/auth/callback`, with no
  wildcard.

### Revoke every client and token

Do this after a security fix, after a PocketID upgrade that rotates its signing
keys, or if a token may have leaked. A new JWT key invalidates every access and
refresh token the gateway has issued. Clearing the OAuth store removes every
registered client and stored PocketID token.

```sh
docker compose stop
openssl rand -base64 48 | sudo tee secrets/jwt_signing_key > /dev/null
docker compose run --rm --no-deps --entrypoint sh duckdb-remote-pocketid-mcp -c 'rm -rf /state/oauth/*'
docker compose up -d
```

Then remove and re-add the connector in Claude.

When you update upstream, the build fails if the new DuckDB lacks a sandbox
setting. Run the tests too (below): they check every behaviour above against the
new versions.

## Development

```sh
uv venv -p 3.13 && uv pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

The tests generate small sample files (JSON lines, CSV, gzip, Parquet, SQLite)
and run real queries through the gateway and the pinned upstream server. They
cover one working query per file type, reads outside the folder (including
`/etc/passwd`), rejected writes and setting changes, row, character and time
limits, and the OIDC wiring against a fake PocketID. The first run downloads the
DuckDB `sqlite` extension.

To try it locally without OAuth:

```sh
python tests/sample_data.py ./sample-data
MCP_AUTH=none DATA_DIR="$PWD/sample-data" DUCKDB_EXTENSION_DIR=/tmp/duckdb-ext \
  DUCKDB_DB_PATH=/tmp/duckdb-empty.duckdb PORT=8000 \
  sh -c '.venv/bin/python scripts/prepare_duckdb.py "$DUCKDB_EXTENSION_DIR" "$DUCKDB_DB_PATH" && .venv/bin/python server.py'
```
