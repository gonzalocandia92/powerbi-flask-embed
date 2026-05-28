"""
Chatbot endpoint — accessible from public report pages without authentication.

Contract:
  POST /chat
  Body:  { "pregunta": "...", "slug": "...", "session_id": 123 }
  200:   { "respuesta": "...", "dax_usado": "...|null", "session_id": 123 }
  400:   { "error": "..." }
  500:   { "respuesta": "...", "dax_usado": null, "session_id": null }

Log endpoints:
  GET /api/chatbot/sessions              — list sessions, newest first
  GET /api/chatbot/sessions/<id>         — session detail + messages
"""
import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from app import db
from app.models import ChatSession, ChatMessage
from app.services.chatbot_service import procesar_pregunta
from app.utils.chatbot_context import get_report_context, get_workspace_info, get_all_active_reports
from app.utils.decorators import retry_on_db_error

bp = Blueprint('chatbot', __name__)


@bp.route('/chat', methods=['POST'])
@retry_on_db_error(max_retries=2, delay=0.5)
def chat():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    pregunta = data.get("pregunta", "").strip()
    if not pregunta:
        return jsonify({"error": "La pregunta no puede estar vacía"}), 400

    slug = data.get("slug", "").strip() or None
    session_id = data.get("session_id") or None
    context = get_report_context(slug) if slug else None
    title = pregunta[:80] + ("…" if len(pregunta) > 80 else "")

    session = None
    try:
        if session_id:
            session = db.session.get(ChatSession, session_id)
        if not session:
            session = ChatSession(slug=slug, title=title)
            db.session.add(session)
            db.session.flush()

        db.session.add(ChatMessage(session_id=session.id, role="user", content=pregunta))

        resultado = procesar_pregunta(pregunta, context=context)

        db.session.add(ChatMessage(
            session_id=session.id,
            role="assistant",
            content=resultado["respuesta"],
            latency_ms=resultado.get("latency_ms"),
            model_used=resultado.get("model"),
            input_tokens=resultado.get("input_tokens"),
            output_tokens=resultado.get("output_tokens"),
            mcp_used=resultado.get("mcp_used"),
            tools_called=resultado.get("tools_called") or None,
            dax_query=resultado.get("dax_usado"),
            had_error=False,
        ))

        session.total_messages += 2
        session.last_message_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify({
            "respuesta": resultado["respuesta"],
            "dax_usado": resultado.get("dax_usado"),
            "session_id": session.id,
        })

    except Exception as e:
        try:
            db.session.rollback()
            if session and session.id:
                db.session.add(ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content=f"Error al procesar la consulta: {str(e)}",
                    had_error=True,
                    error_message=str(e),
                ))
                session.total_messages += 1
                session.had_errors = True
                db.session.commit()
        except Exception:
            db.session.rollback()
            logging.exception("[Chatbot] Failed to log error to DB")

        return jsonify({
            "respuesta": f"Error al procesar la consulta: {str(e)}",
            "dax_usado": None,
            "session_id": session.id if session and session.id else None,
        }), 500


@bp.route('/api/chatbot/context/<slug>', methods=['GET'])
def report_context(slug):
    info = get_workspace_info(slug)
    if not info:
        return jsonify({"error": "Slug not found or inactive"}), 404
    return jsonify(info)


@bp.route('/api/chatbot/reports', methods=['GET'])
def active_reports():
    return jsonify(get_all_active_reports())


@bp.route('/api/chatbot/test-mcp', methods=['POST'])
def test_mcp_log():
    """
    Simulates a full MCP-assisted conversation and logs it to the DB.
    Use this to verify the log tables capture all fields correctly.
    DELETE this endpoint before going to production.
    """
    from datetime import datetime, timezone

    slug = request.get_json(silent=True, force=True) or {}
    slug = slug.get("slug", "test-slug")

    session = ChatSession(
        slug=slug,
        title="Consulta de ventas por región (prueba MCP)",
    )
    db.session.add(session)
    db.session.flush()

    # Mensaje del usuario
    db.session.add(ChatMessage(
        session_id=session.id,
        role="user",
        content="¿Cuáles son las ventas totales por región en el último trimestre?",
    ))

    # Respuesta del asistente simulando MCP con DAX
    db.session.add(ChatMessage(
        session_id=session.id,
        role="assistant",
        content=(
            "Las ventas del último trimestre por región son:\n"
            "• Región Norte: $4.320.000\n"
            "• Región Sur: $2.870.000\n"
            "• Región Centro: $6.150.000\n"
            "El total acumulado es $13.340.000."
        ),
        latency_ms=2340,
        model_used="claude-haiku-4-5-20251001",
        input_tokens=812,
        output_tokens=147,
        mcp_used=True,
        tools_called=[{
            "name": "execute_dax",
            "input": {
                "query": (
                    "EVALUATE SUMMARIZECOLUMNS("
                    "Ventas[Region], "
                    "FILTER(Ventas, Ventas[Trimestre] = \"Q1 2026\"), "
                    "\"Total\", SUM(Ventas[Monto]))"
                )
            }
        }],
        dax_query=(
            "EVALUATE SUMMARIZECOLUMNS("
            "Ventas[Region], "
            "FILTER(Ventas, Ventas[Trimestre] = \"Q1 2026\"), "
            "\"Total\", SUM(Ventas[Monto]))"
        ),
        had_error=False,
    ))

    # Segundo turno — pregunta de seguimiento
    db.session.add(ChatMessage(
        session_id=session.id,
        role="user",
        content="¿Y comparado con el mismo trimestre del año anterior?",
    ))

    # Respuesta con error simulado para probar had_error
    db.session.add(ChatMessage(
        session_id=session.id,
        role="assistant",
        content="No se pudo obtener el dato comparativo: el dataset no contiene información del Q1 2025.",
        latency_ms=1870,
        model_used="claude-haiku-4-5-20251001",
        input_tokens=934,
        output_tokens=62,
        mcp_used=True,
        tools_called=[{
            "name": "execute_dax",
            "input": {"query": "EVALUATE SUMMARIZECOLUMNS(Ventas[Region], FILTER(Ventas, Ventas[Trimestre] = \"Q1 2025\"), \"Total\", SUM(Ventas[Monto]))"}
        }],
        dax_query="EVALUATE SUMMARIZECOLUMNS(Ventas[Region], FILTER(Ventas, Ventas[Trimestre] = \"Q1 2025\"), \"Total\", SUM(Ventas[Monto]))",
        had_error=True,
        error_message="Dataset does not contain data for Q1 2025",
    ))

    session.total_messages = 4
    session.last_message_at = datetime.now(timezone.utc)
    session.had_errors = True
    db.session.commit()

    return jsonify({
        "ok": True,
        "session_id": session.id,
        "detail_url": f"/api/chatbot/sessions/{session.id}",
        "list_url": "/api/chatbot/sessions",
    })


@bp.route('/api/chatbot/sessions', methods=['GET'])
def list_sessions():
    limit = min(int(request.args.get("limit", 50)), 200)
    slug = request.args.get("slug") or None
    q = ChatSession.query.order_by(ChatSession.created_at.desc())
    if slug:
        q = q.filter_by(slug=slug)
    sessions = q.limit(limit).all()
    return jsonify([{
        "id": s.id,
        "slug": s.slug,
        "title": s.title,
        "created_at": s.created_at.isoformat(),
        "last_message_at": s.last_message_at.isoformat(),
        "total_messages": s.total_messages,
        "had_errors": s.had_errors,
    } for s in sessions])


@bp.route('/api/chatbot/sessions/<int:session_id>', methods=['GET'])
def get_session(session_id):
    session = db.session.get(ChatSession, session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    messages = session.messages.order_by(ChatMessage.created_at.asc()).all()
    return jsonify({
        "id": session.id,
        "slug": session.slug,
        "title": session.title,
        "created_at": session.created_at.isoformat(),
        "last_message_at": session.last_message_at.isoformat(),
        "total_messages": session.total_messages,
        "had_errors": session.had_errors,
        "messages": [{
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
        } for m in messages],
    })
