from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")]

import prepare_duckdb  # noqa: E402
from sample_data import make_sample_data  # noqa: E402


@pytest.fixture(scope="session")
def duckdb_assets(tmp_path_factory) -> dict[str, str]:
    """Extension dir and empty database, made the same way as the image build."""
    base = tmp_path_factory.mktemp("duckdb")
    ext, db = str(base / "extensions"), str(base / "empty.duckdb")
    prepare_duckdb.prepare(ext, db)
    return {"DUCKDB_EXTENSION_DIR": ext, "DUCKDB_DB_PATH": db}


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory) -> dict[str, Path]:
    """A data folder plus a secret file next to it that must stay unreadable."""
    base = tmp_path_factory.mktemp("sandbox").resolve()
    data = make_sample_data(base / "data")
    secret = base / "secret.txt"
    secret.write_text("top secret\n")
    # A sibling folder whose name starts with the data folder's name.
    (base / "data-other").mkdir()
    (base / "data-other" / "x.csv").write_text("a\n1\n")
    return {"base": base, "data": data, "secret": secret}


@contextmanager
def patched_env(values: dict[str, str]):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def gateway_env(duckdb_assets, sandbox, **extra) -> dict[str, str]:
    return {
        "MCP_AUTH": "none",
        "DATA_DIR": str(sandbox["data"]),
        **duckdb_assets,
        **{k: str(v) for k, v in extra.items()},
    }


def result_json(result) -> dict:
    """Parse upstream's JSON text result."""
    return json.loads(result.content[0].text)
