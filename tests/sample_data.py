"""Write a small sample data folder: one or more files of each supported type.

Used by the tests. Also handy for a local try-out:

    python tests/sample_data.py ./sample-data
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from pathlib import Path

import duckdb

README = """\
# Sample data

- `events/*.jsonl` and `events/*.jsonl.gz`: one JSON object per line.
  Columns: `ts` (ISO timestamp), `user` (text), `action` (text), `ms` (integer).
- `sales/*.csv` and `sales/*.csv.gz`: columns `region`, `product`, `units`, `price`.
- `metrics/*.parquet`: columns `day` (date), `host` (text), `cpu` (double).
- `app.sqlite`: a SQLite database with tables `users` and `orders`.
"""

EVENTS = [
    [
        {"ts": "2026-11-01T09:00:00", "user": "ana", "action": "login", "ms": 120},
        {"ts": "2026-11-01T09:05:00", "user": "ana", "action": "search", "ms": 340},
        {"ts": "2026-11-01T10:00:00", "user": "ben", "action": "login", "ms": 95},
    ],
    [
        {"ts": "2026-11-02T08:30:00", "user": "cy", "action": "login", "ms": 110},
        {"ts": "2026-11-02T08:31:00", "user": "cy", "action": "export", "ms": 2200},
    ],
]

SALES_HEADER = "region,product,units,price\n"
SALES = [
    "north,widget,10,2.50\nsouth,widget,4,2.50\nnorth,gadget,1,10.00\n",
    "south,gadget,3,10.00\neast,widget,7,2.75\n",
]


def make_sample_data(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(README)

    events = root / "events"
    events.mkdir(exist_ok=True)
    (events / "2026-11-01.jsonl").write_text("".join(json.dumps(e) + "\n" for e in EVENTS[0]))
    with gzip.open(events / "2026-11-02.jsonl.gz", "wt") as f:
        f.writelines(json.dumps(e) + "\n" for e in EVENTS[1])

    sales = root / "sales"
    sales.mkdir(exist_ok=True)
    (sales / "q1.csv").write_text(SALES_HEADER + SALES[0])
    with gzip.open(sales / "q2.csv.gz", "wt") as f:
        f.write(SALES_HEADER + SALES[1])

    metrics = root / "metrics"
    metrics.mkdir(exist_ok=True)
    con = duckdb.connect()
    for i, host in enumerate(("web-1", "web-2")):
        out = str(metrics / f"part-{i}.parquet").replace("'", "''")
        con.execute(
            f"""COPY (SELECT DATE '2026-11-01' + INTERVAL (d) DAY AS day, '{host}' AS host,
                       round(10 + d * 1.5 + {i}, 2)::DOUBLE AS cpu
                FROM range(3) t(d)) TO '{out}' (FORMAT parquet)"""
        )
    con.close()

    db = root / "app.sqlite"
    db.unlink(missing_ok=True)
    s = sqlite3.connect(db)
    s.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, plan TEXT NOT NULL);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, total REAL NOT NULL);
        INSERT INTO users VALUES (1, 'ana', 'pro'), (2, 'ben', 'free'), (3, 'cy', 'pro');
        INSERT INTO orders VALUES (1, 1, 30.0), (2, 1, 12.5), (3, 3, 99.0);
        """
    )
    s.commit()
    s.close()
    return root


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(make_sample_data(Path(sys.argv[1])))
