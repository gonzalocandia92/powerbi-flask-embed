"""Legacy API-key lookup against the Flask-owned database."""

from dataclasses import dataclass
import hashlib
from typing import Optional

from ..db.models import McpAgentConfig
from ..db.session import SessionLocal


@dataclass
class ClientContext:
    client_name: str
    workspace_id: str
    workspace_name: str
    dataset_id: str
    dataset_name: str


def get_client_context(api_key: str) -> Optional[ClientContext]:
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    db = SessionLocal()
    try:
        config = (
            db.query(McpAgentConfig)
            .filter(
                McpAgentConfig.api_key_hash == api_key_hash,
                McpAgentConfig.is_active.is_(True),
            )
            .first()
        )
        if config is None:
            return None
        return ClientContext(
            client_name=(
                f"Empresa_{config.empresa_id}" if config.empresa_id else "Client"
            ),
            workspace_id=config.workspace_id,
            workspace_name=config.workspace_name,
            dataset_id=config.dataset_id,
            dataset_name=config.dataset_name,
        )
    finally:
        db.close()
