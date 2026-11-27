from sqlalchemy import JSON, Column, Integer, String, Table
from .base import metadata


cases = Table(
    "cases", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(48), unique=True, nullable=False),
    Column("title", String(120), nullable=False),
    Column("description", String(420), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("page_size", Integer, nullable=False, server_default="4", default=4),
    Column("created_by", String(48), nullable=False),
)
