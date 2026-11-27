from sqlalchemy import JSON, BigInteger, Column, Integer, String, Table
from .base import metadata


# 教学/验收用主备库状态与故障注入。真实部署可把同一配置放到配置中心或服务发现。
reading_source_controls = Table(
    "reading_source_controls", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope", String(16), nullable=False),  # run / case / global
    Column("run_id", Integer, nullable=True, index=True),
    Column("case_id", Integer, nullable=True, index=True),
    Column("config", JSON(none_as_null=True), nullable=False),
    Column("updated_by", String(48), nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)
