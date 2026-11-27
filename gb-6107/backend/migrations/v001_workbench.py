from sqlalchemy import inspect, text

from src.models import (  # noqa: F401  # 注册全部表到 metadata
    audit,
    cases,
    receipts,
    reviews,
    runs,
    source_controls,
    users,
)
from src.models.base import metadata


_RUN_COLUMNS = {
    "reading_snapshot": "JSON",
    "progress": "JSON",
    "source_config": "JSON",
    "source_report": "JSON",
    "checkpoint": "INTEGER NOT NULL DEFAULT 0",
    "reading_source": "VARCHAR(16)",
    "wait_reason": "VARCHAR(320)",
    "missing_pages": "JSON",
    "idempotency_key": "VARCHAR(80)",
    "claim_token": "VARCHAR(64)",
    "claimed_at": "BIGINT",
}


def _has_column(connection, table_name, column_name):
    return column_name in {column["name"] for column in inspect(connection).get_columns(table_name)}


def upgrade(connection):
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    # 全新库：按当前声明一次建好。
    if not tables:
        metadata.create_all(connection)
        return

    # v001 初始库的增量列。create_all 不会给已存在的 runs 表补列。
    if "cases" in tables and not _has_column(connection, "cases", "page_size"):
        connection.execute(text("ALTER TABLE cases ADD COLUMN page_size INTEGER NOT NULL DEFAULT 4"))

    if "runs" in tables:
        for name, ddl in _RUN_COLUMNS.items():
            if not _has_column(connection, "runs", name):
                connection.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))
        indexes = {index["name"] for index in inspect(connection).get_indexes("runs")}
        if "ix_runs_idempotency_key" not in indexes:
            connection.execute(text("CREATE INDEX ix_runs_idempotency_key ON runs (idempotency_key)"))

    # 新表使用 checkfirst，保证迁移幂等。
    metadata.create_all(connection, tables=[
        receipts.receipts,
        receipts.receipt,
        source_controls.reading_source_controls,
    ])
