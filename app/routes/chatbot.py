"""
Chatbot endpoint accessible from public report pages without authentication.

Contract:
    POST /chat
    Body:  { "message"|"pregunta": "...", "slug": "...", "conversation_id"|"session_id": 123 }
    200:   { "answer": "...", "conversation_id": 123, "report_id": 1, "tool_rounds": 1 }
    400:   { "error": "..." }
    404:   { "error": "..." }
    500:   { "error": "..." }
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from app import db
from app.models import ChatMessage, ChatSession
from app.services import chatbot_service
from app.utils.chatbot_context import get_all_active_reports, get_workspace_info

bp = Blueprint("chatbot", __name__)


@bp.route("/chat", methods=["POST"])
async def chat():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    pregunta = (data.get("message") or data.get("pregunta") or "").strip()
    if not pregunta:
        return jsonify({"error": "La pregunta no puede estar vacia"}), 400

    slug = (data.get("slug") or "").strip() or None
    if not slug:
        return jsonify({"error": "slug is required"}), 400

    conversation_id = data.get("conversation_id") or data.get("session_id")
    conversation_id_value = str(conversation_id).strip() if conversation_id is not None else None
    if not conversation_id_value:
        conversation_id_value = None

    reset_history = bool(data.get("reset_history", False))

    client_ip = request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown"
    client_ip = client_ip.split(",")[0].strip()
    user_key = f"public:{client_ip}"

    try:
        resultado = await chatbot_service.procesar_interaccion_completa(
            pregunta,
            slug=slug,
            user_key=user_key,
            conversation_id=conversation_id_value,
            reset_history=reset_history,
        )
    except chatbot_service.ChatbotNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except chatbot_service.ChatbotLimitExceededError as exc:
        return jsonify({"error": str(exc)}), 403
    except Exception as exc:
        logging.exception("[Chatbot] Failed to process /chat request")
        return jsonify({"error": f"Error al procesar la consulta: {str(exc)}"}), 500

    return jsonify(
        {
            "answer": resultado["answer"],
            "conversation_id": resultado["conversation_id"],
            "report_id": resultado["report_id"],
            "tool_rounds": resultado.get("tool_rounds", 0),
            "dax_query": resultado.get("dax_query"),
        }
    )


@bp.route("/api/chatbot/context/<slug>", methods=["GET"])
def report_context(slug):
    info = get_workspace_info(slug)
    if not info:
        return jsonify({"error": "Slug not found or inactive"}), 404
    return jsonify(info)


@bp.route("/api/chatbot/reports", methods=["GET"])
def active_reports():
    return jsonify(get_all_active_reports())


@bp.route("/api/chatbot/test-agent", methods=["POST"])
def test_agent_log():
    """
    Simula un Flujo de Agente Nativo y guarda mensajes de prueba en la DB.
    Úsalo para verificar que las tablas de log capturan todos los campos correctamente.
    ELIMINA este endpoint antes de ir a producción.
    """
    if not current_app.config.get("TESTING"):
        return jsonify({"error": "Not available"}), 404

    slug = request.get_json(silent=True, force=True) or {}
    slug = slug.get("slug", "test-slug")
    tools_called = [
        {
            "name": "execute_dax_query",
            "input": {
                "dax_query": (
                    "EVALUATE SUMMARIZECOLUMNS("
                    "Ventas[Region], "
                    "FILTER(Ventas, Ventas[Trimestre] = \"Q1 2026\"), "
                    "\"Total\", SUM(Ventas[Monto]))"
                )
            },
        }
    ]

    session = ChatSession(
        slug=slug,
        title="Consulta de ventas por region (prueba de agente)",
    )
    db.session.add(session)
    db.session.flush()

    db.session.add(
        ChatMessage(
            session_id=session.id,
            role="user",
            content="Cuales son las ventas totales por region en el ultimo trimestre?",
        )
    )

    db.session.add(
        ChatMessage(
            session_id=session.id,
            role="assistant",
            content=(
                "Las ventas del ultimo trimestre por region son:\n"
                "• Region Norte: $4.320.000\n"
                "• Region Sur: $2.870.000\n"
                "• Region Centro: $6.150.000\n"
                "El total acumulado es $13.340.000."
            ),
            latency_ms=2340,
            model_used="claude-haiku-4-5-20251001",
            input_tokens=812,
            output_tokens=147,
            mcp_used=bool(tools_called),
            tools_called=tools_called,
            dax_query=(
                "EVALUATE SUMMARIZECOLUMNS("
                "Ventas[Region], "
                "FILTER(Ventas, Ventas[Trimestre] = \"Q1 2026\"), "
                "\"Total\", SUM(Ventas[Monto]))"
            ),
            had_error=False,
        )
    )

    db.session.add(
        ChatMessage(
            session_id=session.id,
            role="user",
            content="Y comparado con el mismo trimestre del ano anterior?",
        )
    )

    db.session.add(
        ChatMessage(
            session_id=session.id,
            role="assistant",
            content="No se pudo obtener el dato comparativo: el dataset no contiene informacion del Q1 2025.",
            latency_ms=1870,
            model_used="claude-haiku-4-5-20251001",
            input_tokens=934,
            output_tokens=62,
            mcp_used=bool(tools_called),
            tools_called=tools_called,
            dax_query=(
                "EVALUATE SUMMARIZECOLUMNS(Ventas[Region], "
                "FILTER(Ventas, Ventas[Trimestre] = \"Q1 2025\"), "
                "\"Total\", SUM(Ventas[Monto]))"
            ),
            had_error=True,
            error_message="Dataset does not contain data for Q1 2025",
        )
    )

    session.total_messages = 4
    session.last_message_at = datetime.now(timezone.utc)
    session.had_errors = True
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "session_id": session.id,
            "detail_url": f"/api/chatbot/sessions/{session.id}",
            "list_url": "/api/chatbot/sessions",
        }
    )


@bp.route("/api/chatbot/sessions", methods=["GET"])
def list_sessions():
    limit = min(int(request.args.get("limit", 50)), 200)
    slug = request.args.get("slug") or None
    q = ChatSession.query.order_by(ChatSession.created_at.desc())
    if slug:
        q = q.filter_by(slug=slug)
    sessions = q.limit(limit).all()
    return jsonify(
        [
            {
                "id": s.id,
                "slug": s.slug,
                "title": s.title,
                "created_at": s.created_at.isoformat(),
                "last_message_at": s.last_message_at.isoformat(),
                "total_messages": s.total_messages,
                "had_errors": s.had_errors,
            }
            for s in sessions
        ]
    )


@bp.route("/api/chatbot/sessions/<int:session_id>", methods=["GET"])
def get_session(session_id):
    session = db.session.get(ChatSession, session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    messages = session.messages.order_by(ChatMessage.created_at.asc()).all()
    return jsonify(
        {
            "id": session.id,
            "slug": session.slug,
            "title": session.title,
            "created_at": session.created_at.isoformat(),
            "last_message_at": session.last_message_at.isoformat(),
            "total_messages": session.total_messages,
            "had_errors": session.had_errors,
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    "latency_ms": m.latency_ms,
                    "model_used": m.model_used,
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                    "mcp_used": m.mcp_used,
                    "tools_called": m.tools_called,
                    "dax_query": m.dax_query,
                    "had_error": m.had_error,
                    "error_message": m.error_message,
                }
                for m in messages
            ],
        }
    )
