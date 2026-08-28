"""Authenticated client for the Flask authorization and Power BI broker."""

from datetime import datetime, timedelta, timezone
import os
import uuid

import httpx
import jwt


BROKER_URL = os.environ["MCP_BROKER_URL"].rstrip("/")
SERVICE_SECRET = os.environ["MCP_INTERNAL_JWT_SECRET"]


class BrokerResponseError(RuntimeError):
    """Public-safe broker failure without leaking the internal broker URL."""

    def __init__(self, error: str, description: str, status_code: int):
        super().__init__(description)
        self.error = error
        self.status_code = status_code


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
    if response.is_error:
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        error_code = str(error_body.get("error") or "broker_request_failed")
        safe_descriptions = {
            "skill_report_context_ambiguous": (
                "El modelo tiene mas de un reporte asociado; configura un reporte "
                "de skills explicito."
            ),
            "skill_report_context_invalid": (
                "El reporte configurado para skills no corresponde al dataset del modelo."
            ),
            "schema_embeddings_unavailable": (
                "No hay un indice de schema disponible; usa get_powerbi_schema."
            ),
            "relevant_schema_unavailable": (
                "No se pudo consultar el indice de schema; usa get_powerbi_schema."
            ),
        }
        description = safe_descriptions.get(
            error_code,
            "No se pudo completar la operacion solicitada.",
        )
        raise BrokerResponseError(error_code, description, response.status_code)
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


async def list_analytics_skills(
    user_token: str, grant_public_id: str, domain_key: str | None = None
) -> dict:
    payload = {"grant_public_id": grant_public_id}
    if domain_key is not None:
        payload["domain_key"] = domain_key
    return await _post("list-skills", user_token, payload)


async def select_analytics_skills(
    user_token: str,
    grant_public_id: str,
    question: str,
    candidate_skill_keys: list[str] | None = None,
) -> dict:
    payload = {"grant_public_id": grant_public_id, "question": question}
    if candidate_skill_keys is not None:
        payload["candidate_skill_keys"] = candidate_skill_keys
    return await _post("select-skills", user_token, payload)


async def get_relevant_schema(
    user_token: str,
    grant_public_id: str,
    question: str,
    selected_skill_keys: list[str] | None = None,
) -> dict:
    payload = {"grant_public_id": grant_public_id, "question": question}
    if selected_skill_keys is not None:
        payload["selected_skill_keys"] = selected_skill_keys
    return await _post("relevant-schema", user_token, payload)


async def audit_result(
    user_token: str, grant_public_id: str, **details
) -> None:
    await _post(
        "audit-result",
        user_token,
        {"grant_public_id": grant_public_id, **details},
    )
