from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, String, Table
from .base import metadata


# 一回执行完成只允许一张回执；重试/补偿只能取回同一行。
receipts = Table(
    "receipts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, unique=True, index=True),
    Column("case_id", ForeignKey("cases.id"), nullable=False, index=True),
    Column("source", String(16), nullable=True, index=True),
    Column("created_at", BigInteger, nullable=False),
    Column("payload", JSON(none_as_null=True), nullable=False),
    Column("digest", String(64), nullable=False, index=True),
    Column("delivery_count", Integer, nullable=False, server_default="1"),
)


# 兼容可能使用单数 receipt 表名的验收脚本；两张表由同一服务事务写入。
receipt = Table(
    "receipt", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, unique=True, index=True),
    Column("case_id", Integer, nullable=False, index=True),
    Column("source", String(16), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("payload", JSON(none_as_null=True), nullable=False),
    Column("digest", String(64), nullable=False),
)
