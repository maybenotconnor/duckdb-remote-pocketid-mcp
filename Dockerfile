# syntax=docker/dockerfile:1
#
# One container: the pinned upstream mcp-server-motherduck (stdio child) and a
# FastMCP gateway that serves it over streamable HTTP behind PocketID OAuth.
# To update upstream, change its line in requirements.txt and rebuild.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /app/requirements.txt
COPY scripts/prepare_duckdb.py /app/scripts/prepare_duckdb.py

# Extensions download here, at build time. At runtime the server installs no
# extensions: install and autoload are off and the configuration is locked.
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && python /app/scripts/prepare_duckdb.py /opt/duckdb/extensions /opt/duckdb/empty.duckdb \
 && chmod -R a-w /opt/duckdb \
 && useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin duck \
 && mkdir -p /state/oauth /data \
 && chown -R duck:duck /state

COPY server.py /app/server.py

ENV DATA_DIR=/data \
    DUCKDB_DB_PATH=/opt/duckdb/empty.duckdb \
    DUCKDB_EXTENSION_DIR=/opt/duckdb/extensions \
    OAUTH_STORAGE_DIR=/state/oauth \
    HOME=/nonexistent

USER duck
WORKDIR /app
VOLUME ["/state"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"

CMD ["python", "/app/server.py"]
