"""Prepare DuckDB at image build time.

- Installs the extensions the server loads at runtime into EXTENSION_DIR.
  json and parquet are built into the DuckDB Python wheel; sqlite is not.
- Creates the empty database file that upstream opens read-only.
- Fails if the pinned DuckDB lacks any setting that server.build_init_sql uses,
  so an upstream bump that changes DuckDB cannot silently weaken the sandbox.

Usage: python scripts/prepare_duckdb.py EXTENSION_DIR DB_PATH
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

EXTENSIONS = ("sqlite",)
BUILT_IN = ("json", "parquet")
REQUIRED_SETTINGS = (
    "extension_directory",
    "autoinstall_known_extensions",
    "autoload_known_extensions",
    "allow_community_extensions",
    "allow_persistent_secrets",
    "memory_limit",
    "allowed_directories",
    "enable_external_access",
    "lock_configuration",
)


def prepare(extension_dir: str, db_path: str) -> None:
    Path(extension_dir).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    quoted = "'" + extension_dir.replace("'", "''") + "'"
    con.execute(f"SET extension_directory = {quoted}")

    names = {row[0] for row in con.execute("SELECT name FROM duckdb_settings()").fetchall()}
    missing = [s for s in REQUIRED_SETTINGS if s not in names]
    if missing:
        sys.exit(f"DuckDB {duckdb.__version__} lacks settings: {', '.join(missing)}")

    for ext in EXTENSIONS:
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")

    loaded = {
        row[0]
        for row in con.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded").fetchall()
    }
    want = {"sqlite_scanner" if e == "sqlite" else e for e in EXTENSIONS} | set(BUILT_IN)
    if not want <= loaded:
        sys.exit(f"DuckDB {duckdb.__version__}: extensions not loaded: {', '.join(sorted(want - loaded))}")
    con.close()

    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        duckdb.connect(str(db)).close()
    print(f"DuckDB {duckdb.__version__}: extensions in {extension_dir}, empty database at {db}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    prepare(sys.argv[1], sys.argv[2])
