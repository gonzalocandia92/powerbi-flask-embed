"""Minimal read-only mapping for Flask-owned legacy MCP configuration."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, Column, DateTime, String

from .session import Base


def _utcnow():
    return datetime.now(timezone.utc)


class McpAgentConfig(Base):
    __tablename__ = "mcp_agent_configs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    api_key_hash = Column(String(256), unique=True, nullable=True, index=True)
    workspace_id = Column(String(120), nullable=False)
    workspace_name = Column(String(200), nullable=False)
    dataset_id = Column(String(120), nullable=False)
    dataset_name = Column(String(200), nullable=False)
    empresa_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
