"""ASGI entrypoint exposing OAuth MCP and a temporary legacy API-key route."""

from contextlib import asynccontextmanager
import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth.api_keys import get_client_context
from .auth.oauth import create_auth_provider
from .legacy_tools import legacy_api_key_var, legacy_mcp
from .powerbi.xmla_tom import tom_runtime_status
from .tools import create_mcp


def _csv_env(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


public_url = urlsplit(os.environ["MCP_PUBLIC_URL"].rstrip("/"))
if (
    public_url.scheme not in {"http", "https"}
    or not public_url.hostname
    or public_url.query
    or public_url.fragment
):
    raise RuntimeError(
        "MCP_PUBLIC_URL must be an absolute HTTP(S) URL without query or fragment"
    )
public_origin = f"{public_url.scheme}://{public_url.netloc}"
allowed_hosts = [public_url.hostname, *_csv_env("MCP_ALLOWED_HOSTS")]
allowed_origins = [public_origin, *_csv_env("MCP_ALLOWED_ORIGINS")]


oauth_mcp = create_mcp(create_auth_provider())
oauth_http_app = oauth_mcp.http_app(
    path="/",
    transport="streamable-http",
    host_origin_protection=True,
    allowed_hosts=allowed_hosts,
    allowed_origins=allowed_origins,
)
legacy_http_app = legacy_mcp.http_app(
    path="/sse",
    transport="streamable-http",
    host_origin_protection=True,
    allowed_hosts=allowed_hosts,
    allowed_origins=allowed_origins,
)


@asynccontextmanager
async def lifespan(_app):
    async with oauth_http_app.lifespan(_app):
        async with legacy_http_app.lifespan(_app):
            yield


app = FastAPI(title="MCP Power BI Aklara", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Mcp-Protocol-Version",
        "Mcp-Session-Id",
    ],
)


class LegacyAPIKeyMiddleware:
    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if headers.get(b"x-forwarded-proto") == b"https":
                scope["scheme"] = "https"
            parts = scope.get("path", "").strip("/").split("/")
            first_part = parts[0] if parts else ""
            is_legacy_endpoint = first_part.startswith("mcp-key-") or (
                len(parts) == 2 and parts[1] == "sse"
            )
            if is_legacy_endpoint:
                if os.getenv("MCP_ENABLE_LEGACY_API_KEYS", "true").lower() != "true":
                    return await self._deny(send, b"Legacy API keys disabled")
                if not first_part.startswith("mcp-key-"):
                    return await self._deny(send, b"Invalid API key")
                if not get_client_context(first_part):
                    return await self._deny(send, b"Invalid API key")
                token = legacy_api_key_var.set(first_part)
                try:
                    return await self.app(scope, receive, send)
                finally:
                    legacy_api_key_var.reset(token)
        return await self.app(scope, receive, send)

    async def _deny(self, send, message):
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"' + message + b'"}',
            }
        )


app.add_middleware(LegacyAPIKeyMiddleware)


@app.get("/")
async def health():
    tom_ready, _ = tom_runtime_status()
    if not tom_ready:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "oauth_mcp": "/mcp",
                "tom": "unavailable",
            },
        )
    return {"status": "ok", "oauth_mcp": "/mcp", "tom": "ready"}


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    return {
        "resource": os.environ["MCP_PUBLIC_URL"].rstrip("/"),
        "authorization_servers": [os.environ["MCP_OAUTH_ISSUER"].rstrip("/")],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [
            "mcp:models:list",
            "mcp:models:read",
            "mcp:models:query",
            "mcp:models:write",
        ],
    }


app.mount("/mcp", oauth_http_app)
app.mount("/{api_key}", legacy_http_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "services.mcp_aklara.src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
