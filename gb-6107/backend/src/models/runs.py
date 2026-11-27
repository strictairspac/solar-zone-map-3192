from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, String, Table, UniqueConstraint
from .base import metadata


runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("case_id", ForeignKey("cases.id"), nullable=False, index=True),
    Column("status", String(16), nullable=False, index=True),
    Column("parameter", Integer, nullable=False),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger, nullable=True),
    Column("result", JSON(none_as_null=True), nullable=True),
    Column("failure", String(320), nullable=True),
    Column("digest", String(64), nullable=True),
    # 主备故障切换：接单时钉住的读数快照、页级检查点和实际来源。
    Column("reading_snapshot", JSON(none_as_null=True), nullable=True),
    Column("progress", JSON(none_as_null=True), nullable=True),
    Column("source_config", JSON(none_as_null=True), nullable=True),
    Column("source_report", JSON(none_as_null=True), nullable=True),
    Column("checkpoint", Integer, nullable=False, server_default="0"),
    Column("reading_source", String(16), nullable=True),
    Column("wait_reason", String(320), nullable=True),
    Column("missing_pages", JSON(none_as_null=True), nullable=True),
    Column("idempotency_key", String(80), nullable=True, index=True),
    Column("claim_token", String(64), nullable=True),
    Column("claimed_at", BigInteger, nullable=True),
    UniqueConstraint("actor", "idempotency_key", name="uq_runs_actor_idempotency"),
)
