"""Authenticated client for the Flask authorization and Power BI broker."""

from datetime import datetime, timedelta, timezone
import os
import uuid

import httpx
import jwt


BROKER_URL = os.environ["MCP_BROKER_URL"].rstrip("/")
SERVICE_SECRET = os.environ["MCP_INTERNAL_JWT_SECRET"]


def _service_token() -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": "mcp-aklara",
            "sub": os.getenv("MCP_SERVICE_ID", "mcp-aklara"),
            "aud": "powerbi-flask-internal",
            "iat": now,
            "exp": now + timedelta(seconds=60),
            "jti": str(uuid.uuid4()),
        },
        SERVICE_SECRET,
        algorithm="HS256",
    )


def _headers(user_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_service_token()}",
        "X-MCP-User-Token": f"Bearer {user_token}",
    }


async def _post(path: str, user_token: str, payload: dict | None = None) -> dict:
    cert_path = os.getenv("MCP_INTERNAL_CLIENT_CERT")
    key_path = os.getenv("MCP_INTERNAL_CLIENT_KEY")
    cert = (cert_path, key_path) if cert_path and key_path else None
    async with httpx.AsyncClient(timeout=90, cert=cert) as client:
        response = await client.post(
            f"{BROKER_URL}/internal/mcp/{path}",
            headers=_headers(user_token),
            json=payload or {},
        )
    response.raise_for_status()
    return response.json() if response.content else {}


async def validate_token(user_token: str) -> dict:
    return await _post("validate-token", user_token)


async def list_access(user_token: str) -> list[dict]:
    return (await _post("list-access", user_token)).get("models", [])


async def resolve_access(
    user_token: str, grant_public_id: str, required_permission: str
) -> dict:
    return await _post(
        "resolve-access",
        user_token,
        {
            "grant_public_id": grant_public_id,
            "required_permission": required_permission,
        },
    )


async def audit_result(
    user_token: str, grant_public_id: str, **details
) -> None:
    await _post(
        "audit-result",
        user_token,
        {"grant_public_id": grant_public_id, **details},
    )
