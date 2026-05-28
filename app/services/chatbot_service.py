"""
Chatbot service — thin proxy between Flask and the Claude API.

Architecture:
  Browser → POST /chat (Flask) → Claude API + MCP server → response

When CHATBOT_MCP_URL is set, Claude receives the other dev's MCP server and can
call its tools (DAX queries, schema lookups, etc.) automatically.
When it's not set, Claude still works but can only reason over the context passed in.

The Anthropic client is created once and reused across requests.
"""
import os
import time
import logging

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _anthropic = None  # type: ignore
    _ANTHROPIC_AVAILABLE = False

_claude_client = None


def _get_client():
    global _claude_client
    if not _ANTHROPIC_AVAILABLE:
        return None
    if _claude_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        _claude_client = _anthropic.Anthropic(api_key=api_key)
    return _claude_client


def _build_system(extra_context: str | None) -> str:
    base = (
        "Eres KLARA, analista de datos de Sudata. "
        "Respondé siempre en español, de forma clara y ejecutiva. "
        "Cuando des números de dinero usá formato con puntos (ej: $1.700.000). "
        "Nunca uses markdown (asteriscos, almohadillas, guiones bajos). "
        "NUNCA inventes datos: usá solo lo que devuelvan las herramientas disponibles."
    )
    if extra_context:
        base += f"\n\nContexto del tablero actual:\n{extra_context}"
    return base


def procesar_pregunta(pregunta: str, context: str | None = None) -> dict:
    """
    Send a question to Claude and return its response plus telemetry.

    Args:
        pregunta: Natural-language question from the user.
        context:  Optional string describing the active report/dataset context.

    Returns:
        dict with keys:
          - respuesta (str): Claude's answer in Spanish.
          - dax_usado (str|None): DAX query used, if any.
          - latency_ms (int): Round-trip time to the Claude API in milliseconds.
          - model (str): Model ID that was used.
          - input_tokens (int|None): Tokens consumed by the prompt.
          - output_tokens (int|None): Tokens generated in the response.
          - mcp_used (bool): Whether the MCP path was taken.
          - tools_called (list): Tool-use blocks returned by the model.
    """
    client = _get_client()
    if client is None:
        return {
            "respuesta": "El chatbot no está habilitado (ANTHROPIC_API_KEY no configurada).",
            "dax_usado": None,
            "latency_ms": 0,
            "model": None,
            "input_tokens": None,
            "output_tokens": None,
            "mcp_used": False,
            "tools_called": [],
        }

    model = os.getenv("CHATBOT_MODEL", "claude-haiku-4-5-20251001")
    system_prompt = _build_system(context)
    messages = [{"role": "user", "content": pregunta}]
    mcp_url = os.getenv("CHATBOT_MCP_URL")
    t0 = time.monotonic()

    # Mock MCP mode — simulates a full MCP tool call without a real server
    if os.getenv("CHATBOT_MOCK_MCP", "").lower() == "true":
        time.sleep(0.8)
        fake_dax = (
            f"EVALUATE SUMMARIZECOLUMNS("
            f"Ventas[Region], "
            f"\"Total\", SUM(Ventas[Monto]))"
        )
        return {
            "respuesta": (
                f"(SIMULACIÓN MCP) Basándome en los datos del tablero, "
                f"las ventas totales son $13.340.000 distribuidas en 3 regiones. "
                f"Pregunta recibida: \"{pregunta}\""
            ),
            "dax_usado": fake_dax,
            "latency_ms": 800,
            "model": model,
            "input_tokens": 512,
            "output_tokens": 128,
            "mcp_used": True,
            "tools_called": [{"name": "execute_dax", "input": {"query": fake_dax}}],
        }

    try:
        if mcp_url:
            mcp_server: dict = {"type": "url", "url": mcp_url, "name": "data"}
            mcp_api_key = os.getenv("CHATBOT_MCP_API_KEY")
            if mcp_api_key:
                mcp_server["authorization_token"] = mcp_api_key

            response = client.beta.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                mcp_servers=[mcp_server],
                messages=messages,
                betas=["mcp-client-2025-04-04"],
            )
        else:
            logging.warning("[Chatbot] CHATBOT_MCP_URL not set — running without data tools.")
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt + (
                    "\n\nNOTA: Las herramientas de datos no están disponibles en este momento. "
                    "Indicá al usuario que el sistema está siendo configurado."
                ),
                messages=messages,
            )

        latency_ms = int((time.monotonic() - t0) * 1000)

        respuesta = next((b.text for b in response.content if hasattr(b, "text")), "")

        dax_usado = None
        tools_called = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use":
                tool_entry = {"name": block.name, "input": block.input}
                tools_called.append(tool_entry)
                logging.debug(f"[Chatbot] MCP tool called: {block.name} input={block.input}")
                if isinstance(block.input, dict) and "query" in block.input:
                    dax_usado = block.input["query"]

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)

        logging.debug(
            f"[Chatbot] pregunta={pregunta!r} mcp={bool(mcp_url)} "
            f"latency={latency_ms}ms tokens={input_tokens}+{output_tokens}"
        )

        return {
            "respuesta": respuesta,
            "dax_usado": dax_usado,
            "latency_ms": latency_ms,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "mcp_used": bool(mcp_url),
            "tools_called": tools_called,
        }

    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        name = type(e).__name__
        if "AuthenticationError" in name:
            raise Exception("ANTHROPIC_API_KEY inválida.")
        if "RateLimitError" in name:
            raise Exception("Límite de requests alcanzado. Esperá unos segundos.")
        if "APIConnectionError" in name:
            raise Exception("No se pudo conectar a la API de Claude.")
        raise
