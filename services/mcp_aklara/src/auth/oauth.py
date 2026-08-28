"""FastMCP resource-server authentication backed by Flask OAuth."""

import os

from fastmcp.server.auth import JWTVerifier, RemoteAuthProvider

from .broker import validate_token


class BrokerJWTVerifier(JWTVerifier):
    async def verify_token(self, token: str):
        access = await super().verify_token(token)
        if access is None:
            return None
        try:
            await validate_token(token)
        except Exception:
            return None
        return access


def create_auth_provider() -> RemoteAuthProvider:
    issuer = os.environ["MCP_OAUTH_ISSUER"].rstrip("/")
    public_url = os.environ["MCP_PUBLIC_URL"].rstrip("/")
    verifier = BrokerJWTVerifier(
        jwks_uri=f"{issuer}/.well-known/jwks.json",
        issuer=issuer,
        audience=public_url,
        algorithm="RS256",
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[issuer],
        base_url=public_url,
        scopes_supported=[
            "mcp:models:list",
            "mcp:models:read",
            "mcp:models:query",
            "mcp:models:write",
        ],
        resource_name="Aklara Power BI MCP",
    )
