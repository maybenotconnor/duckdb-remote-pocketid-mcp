"""DuckDB layer on its own: the init SQL, without the gateway's SELECT check.

These run the exact SQL the server passes to upstream with --init-sql, on the
same kind of read-only empty database, so they prove the DuckDB layer holds
even if the gateway check were bypassed.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

import server


@pytest.fixture
def con(duckdb_assets, sandbox):
    c = duckdb.connect(duckdb_assets["DUCKDB_DB_PATH"], read_only=True)
    c.execute(server.build_init_sql(sandbox["data"], duckdb_assets["DUCKDB_EXTENSION_DIR"]))
    yield c
    c.close()


def blocked(con, sql: str) -> str:
    with pytest.raises(duckdb.Error) as e:
        con.execute(sql).fetchall()
    return str(e.value)


def test_settings_are_locked_in(con, sandbox):
    rows = dict(con.execute(
        "SELECT name, value FROM duckdb_settings() WHERE name IN "
        "('enable_external_access', 'lock_configuration', 'allowed_directories', "
        "'autoinstall_known_extensions', 'autoload_known_extensions', 'temp_directory')"
    ).fetchall())
    assert rows["enable_external_access"] == "false"
    assert rows["lock_configuration"] == "true"
    assert rows["autoinstall_known_extensions"] == "false"
    assert rows["autoload_known_extensions"] == "false"
    # Exactly the data folder: no spill folder sneaks in.
    assert rows["allowed_directories"] == f"[{sandbox['data']}/]"
    assert rows["temp_directory"] == ""


def test_reads_inside_data_folder(con, sandbox):
    assert con.execute(f"SELECT count(*) FROM read_csv('{sandbox['data']}/sales/*.csv*')").fetchone() == (5,)


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "{base}/secret.txt",
    "{data}/../secret.txt",
    "{data}/events/../../secret.txt",
    "{base}/data-other/x.csv",
])
def test_reads_outside_data_folder_fail(con, sandbox, path):
    path = path.format(base=sandbox["base"], data=sandbox["data"])
    for fn in ("read_csv", "read_text", "read_blob"):
        assert "Permission Error" in blocked(con, f"SELECT * FROM {fn}('{path}')")


def test_symlink_out_of_data_folder_fails(con, sandbox, tmp_path):
    link = sandbox["data"] / "escape"
    if not link.exists():
        link.symlink_to(sandbox["base"])
    try:
        assert "Permission Error" in blocked(con, f"SELECT * FROM read_text('{link}/secret.txt')")
    finally:
        link.unlink()


def test_glob_outside_fails(con):
    assert "Permission Error" in blocked(con, "SELECT * FROM glob('/etc/*')")


@pytest.mark.parametrize("sql", [
    "SET enable_external_access = true",
    "RESET enable_external_access",
    "SET lock_configuration = false",
    "RESET lock_configuration",
    "SET allowed_directories = ['/']",
    "PRAGMA enable_external_access = true",
    "SET autoload_known_extensions = true",
])
def test_settings_cannot_change(con, sql):
    assert "configuration has been locked" in blocked(con, sql)


@pytest.mark.parametrize("sql", ["INSTALL httpfs", "LOAD httpfs", "FORCE INSTALL sqlite"])
def test_no_extension_install_or_load(con, sql):
    msg = blocked(con, sql)
    assert "Permission Error" in msg or "disabled" in msg


@pytest.mark.parametrize("url", ["https://example.com/a.csv", "http://127.0.0.1:9/a.csv", "s3://bucket/a.csv"])
def test_no_network_reads(con, url):
    blocked(con, f"SELECT * FROM read_csv('{url}')")


def test_main_database_is_read_only(con):
    assert "read-only" in blocked(con, "CREATE TABLE t (a INT)")


def test_writes_outside_data_folder_fail(con, tmp_path):
    target = tmp_path / "out.csv"
    assert "Permission Error" in blocked(con, f"COPY (SELECT 1) TO '{target}'")
    assert not target.exists()


def test_duckdb_alone_allows_copy_into_data_folder(con, sandbox):
    """Why the other two layers exist: allowed_directories grants write too.

    The gateway's SELECT-only check and the read-only mount both block this.
    This test documents the DuckDB behaviour so an upgrade that changes it is noticed.
    """
    target: Path = sandbox["data"] / "should-not-exist.csv"
    try:
        con.execute(f"COPY (SELECT 1) TO '{target}'")
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_duckdb_shares_a_locked_instance_between_open_connections(duckdb_assets, sandbox):
    """Why the gateway runs one query at a time.

    A second connection to a file that is already open gets the same,
    already-locked instance, so upstream's per-query init SQL fails on it.
    Once every connection is closed, the next one gets a fresh instance.
    """
    db, ext = duckdb_assets["DUCKDB_DB_PATH"], duckdb_assets["DUCKDB_EXTENSION_DIR"]
    init = server.build_init_sql(sandbox["data"], ext)
    first = duckdb.connect(db, read_only=True)
    first.execute(init)
    second = duckdb.connect(db, read_only=True)
    try:
        with pytest.raises(duckdb.Error, match="configuration has been locked"):
            second.execute(init)
    finally:
        second.close()
        first.close()
    fresh = duckdb.connect(db, read_only=True)
    try:
        fresh.execute(init)  # works again
    finally:
        fresh.close()
