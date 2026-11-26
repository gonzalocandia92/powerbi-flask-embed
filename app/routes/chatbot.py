"""
Chatbot endpoint accessible from public report pages without authentication.

Contract:
    POST /chat
    Body:  { "message"|"pregunta": "...", "slug": "...", "conversation_id"|"session_id": 123 }
    200:   { "answer": "...", "conversation_id": 123, "diagnostic_id": "opaque", "tool_rounds": 1 }
    400:   { "error": "..." }
    404:   { "error": "..." }
    403/503: { "error": "...", "code": "chatbot_disabled"|"chatbot_unavailable" }
    500:   { "error": "..." }   (no internal details; "diagnostic_id" when recorded)

    POST /chat/runs                (same body)  -> 202 {run_id, status, stage, poll_after_ms}
                                               409 {code: chat_run_in_progress, run_id} | 503 {code: chat_busy}
    GET  /chat/runs/<run_id>                   -> 200 {run_id, status, stage, poll_after_ms? | result? | error?}
    POST /chat/runs/<run_id>/cancel            -> 200 {run_id, status, ...} | 409 {code: chat_run_finished}
    404 for an unknown run AND for a run of another owner (indistinguishable on purpose).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, current_app, g, jsonify, request
from flask_login import current_user

from app import db, limiter
from app.models import ChatMessage, ChatSession, PublicLink
from app.persistence import chat_run_states
from app.services import ai_billing, chat_public, chatbot_service, model_catalog, session_owner
from app.services.chat_runs import LIMIT_OWNER_OPEN_RUNS, ChatRunLimitError, ChatRunRequest, ChatRunStore
from app.services.chat_runs.payloads import run_payload
from app.services.chat_runs.settings import ChatRunApiSettings
from app.services.report_access import (
    access_denied_json,
    active_link_report,
    has_backoffice_access,
    is_staff_actor,
    user_can_view_report,
)
from app.utils.chatbot_context import (
    DatasetResolutionError,
    get_all_active_reports,
    get_workspace_info,
)

bp = Blueprint("chatbot", __name__)


@bp.after_request
def _attach_owner_cookie(response):
    """Set the signed visitor cookie issued while handling /chat (if any)."""
    return session_owner.attach_owner_cookie(response, g.get("klara_owner_cookie"))


def _slug_access_error(slug):
    """Error response when `slug` belongs to a report that requires a login the caller lacks."""
    try:
        report = active_link_report(slug)
        if report is None or user_can_view_report(current_user, report):
            return None
        return access_denied_json(current_user)
    except Exception:
        return None


def _backoffice_required():
    """Error response unless the caller is a logged-in backoffice user.

    Chat transcripts, report inventories and Power BI identifiers are internal
    data: they are never exposed to anonymous or merely authenticated callers.
    """
    if has_backoffice_access(current_user):
        return None
    return access_denied_json(current_user)


@dataclass(frozen=True)
class _ChatInput:
    question: str
    slug: str
    conversation_id: Optional[str]
    reset_history: bool
    model_key: Optional[str]
    user_key: str


def _read_chat_input():
    """``(input, None)`` or ``(None, error response)``: the body contract shared by ``/chat`` and ``/chat/runs``."""
    data = request.get_json(silent=True)
    if not data:
        return None, (jsonify({"error": "Request body must be JSON"}), 400)

    pregunta = (data.get("message") or data.get("pregunta") or "").strip()
    if not pregunta:
        return None, (jsonify({"error": "La pregunta no puede estar vacia"}), 400)

    slug = (data.get("slug") or "").strip() or None
    if not slug:
        return None, (jsonify({"error": "slug is required"}), 400)

    conversation_id = data.get("conversation_id") or data.get("session_id")
    conversation_id_value = str(conversation_id).strip() if conversation_id is not None else None

    client_ip = request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown"
    client_ip = client_ip.split(",")[0].strip()
    return _ChatInput(
        question=pregunta,
        slug=slug,
        conversation_id=conversation_id_value or None,
        reset_history=bool(data.get("reset_history", False)),
        model_key=(data.get("model_key") or "").strip() or None,
        user_key=f"public:{client_ip}",
    ), None


@bp.route("/chat", methods=["POST"])
async def chat():
    chat_input, error_response = _read_chat_input()
    if error_response is not None:
        return error_response

    denied = _slug_access_error(chat_input.slug)
    if denied is not None:
        return denied

    # The conversation is bound to who started it (signed visitor cookie or
    # logged-in user). The cookie is issued on the first turn of a visitor.
    owner_hash, g.klara_owner_cookie = session_owner.resolve_web_owner(current_user)

    try:
        resultado = await chatbot_service.procesar_interaccion_completa(
            chat_input.question,
            slug=chat_input.slug,
            user_key=chat_input.user_key,
            conversation_id=chat_input.conversation_id,
            reset_history=chat_input.reset_history,
            model_key=chat_input.model_key,
            is_staff=is_staff_actor(current_user),
            owner_hash=owner_hash,
        )
    except Exception as exc:
        # One mapping for this route and the async chat worker (``chat_public``). The opaque
        # ``diagnostic_id`` is the only support handle; internal details stay in the log.
        error = chat_public.public_error(exc)
        if error.unexpected:
            logging.exception("[Chatbot] Failed to process /chat request")
        return jsonify(error.body()), error.http_status

    # The public answer never carries internal identifiers (report, dataset, workspace).
    return jsonify(chat_public.public_payload(resultado))


# ── Asynchronous chat (docs/klara-chat-asincrono.md) ─────────────────────────
#
# POST /chat/runs enqueues the question and answers at once (202); the ``chat-worker`` runs the same turn
# ``/chat`` runs; the browser polls GET /chat/runs/<id>. Every response is JSON, errors included, and a run is
# only visible to (and cancellable by) the owner that created it: anyone else gets the same 404 as for an id
# that does not exist.

RUNS_PATH = "/chat/runs"
_NOT_FOUND = {"error": "Consulta no encontrada.", "code": "not_found"}
_NO_STORE = {"Cache-Control": "no-store"}
_GENERIC_FAILURE = {"error": chat_public.GENERIC_ERROR_MESSAGE, "code": "processing_error"}


def _api_settings() -> ChatRunApiSettings:
    return ChatRunApiSettings.from_env()


def _run_rate_limit() -> str:
    return _api_settings().rate_limit


def _public_failure(exc):
    error = chat_public.public_error(exc)
    return jsonify({**error.body(), "code": error.code}), error.http_status


@bp.route(RUNS_PATH, methods=["POST"])
@limiter.limit(_run_rate_limit)
def create_chat_run():
    chat_input, error_response = _read_chat_input()
    if error_response is not None:
        return error_response
    settings = _api_settings()
    if len(chat_input.question) > settings.max_question_chars:
        return jsonify({"error": f"La pregunta supera los {settings.max_question_chars} caracteres.",
                        "code": "question_too_long"}), 400

    denied = _slug_access_error(chat_input.slug)
    if denied is not None:
        return denied
    # Fail fast instead of queueing a run that can only fail (unknown slugs could also flood the queue).
    report = active_link_report(chat_input.slug)
    if report is None:
        return _public_failure(chatbot_service.ChatbotNotFoundError("Reporte no encontrado."))
    if not report.chatbot_enabled:
        return _public_failure(chatbot_service.ChatbotDisabledError(chatbot_service.DISABLED_MESSAGE))

    owner_hash, g.klara_owner_cookie = session_owner.resolve_web_owner(current_user)
    try:
        run = ChatRunStore().enqueue(
            ChatRunRequest(
                slug=chat_input.slug, question=chat_input.question, owner_hash=owner_hash,
                conversation_id=chat_input.conversation_id, model_key=chat_input.model_key,
                reset_history=chat_input.reset_history, is_staff=is_staff_actor(current_user),
                user_key=chat_input.user_key, report_id=report.id,
            ),
            max_open_per_owner=settings.max_open_per_owner,
            max_queued=settings.max_queued,
        )
    except ChatRunLimitError as exc:
        if exc.kind == LIMIT_OWNER_OPEN_RUNS:
            return jsonify({"error": "Ya hay una consulta en curso.", "code": "chat_run_in_progress",
                            "run_id": exc.open_run_id}), 409, _NO_STORE
        return (jsonify({"error": "KLARA está atendiendo muchas consultas. Probá de nuevo en unos segundos.",
                         "code": "chat_busy"}), 503, {**_NO_STORE, "Retry-After": "5"})
    except Exception:
        logging.exception("[Chatbot] Failed to enqueue a chat run")
        return jsonify(_GENERIC_FAILURE), 500, _NO_STORE

    headers = {**_NO_STORE, "Location": f"{RUNS_PATH}/{run.id}"}
    return jsonify(run_payload(run, poll_after_ms=settings.poll_after_ms)), 202, headers


@bp.route(f"{RUNS_PATH}/<run_id>", methods=["GET"])
def get_chat_run(run_id):
    try:
        run = ChatRunStore().get_for_owner(run_id, session_owner.presented_web_owner(current_user))
        if run is None:
            return jsonify(_NOT_FOUND), 404, _NO_STORE
        return jsonify(run_payload(run, poll_after_ms=_api_settings().poll_after_ms)), 200, _NO_STORE
    except Exception:
        logging.exception("[Chatbot] Failed to read a chat run")
        return jsonify(_GENERIC_FAILURE), 500, _NO_STORE


@bp.route(f"{RUNS_PATH}/<run_id>/cancel", methods=["POST"])
def cancel_chat_run(run_id):
    owner = session_owner.presented_web_owner(current_user)
    try:
        store = ChatRunStore()
        run = store.get_for_owner(run_id, owner)
        if run is None:
            return jsonify(_NOT_FOUND), 404, _NO_STORE
        if run.status in chat_run_states.TERMINAL_STATUSES:
            return jsonify({**run_payload(run, poll_after_ms=0), "error": "La consulta ya terminó.",
                            "code": "chat_run_finished"}), 409, _NO_STORE
        store.request_cancel(run_id, owner)
        db.session.expire_all()
        run = store.get_for_owner(run_id, owner)
        return jsonify(run_payload(run, poll_after_ms=_api_settings().poll_after_ms)), 200, _NO_STORE
    except Exception:
        logging.exception("[Chatbot] Failed to cancel a chat run")
        return jsonify(_GENERIC_FAILURE), 500, _NO_STORE


@bp.errorhandler(429)
def _rate_limited(error):
    return jsonify({"error": "Demasiadas consultas seguidas. Esperá un momento.", "code": "rate_limited"}), 429


@bp.app_errorhandler(404)
@bp.app_errorhandler(405)
def _json_errors_for_chat_runs(error):
    """Routing errors under /chat/runs keep the JSON contract; every other path keeps Flask's default."""
    if not request.path.startswith(RUNS_PATH):
        return error
    code = "not_found" if error.code == 404 else "method_not_allowed"
    return jsonify({"error": error.description or code, "code": code}), error.code


@bp.route("/api/chatbot/models", methods=["GET"])
def chat_models():
    """Return only evaluated models permitted for the report's effective scope."""
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug is required"}), 400
    link = PublicLink.query.filter_by(custom_slug=slug, is_active=True).first()
    if link is None:
        return jsonify({"error": "Slug not found or inactive"}), 404
    report = link.report
    if not user_can_view_report(current_user, report):
        return access_denied_json(current_user)
    billing = ai_billing.resolve_report_billing_context(report)
    models = model_catalog.available_client_models(
        report_id=report.id, empresa_id=billing.empresa_id, config=dict(current_app.config)
    )
    owner_hash, _unused_cookie = session_owner.resolve_web_owner(current_user)
    selected_key = model_catalog.session_model_key(
        request.args.get("conversation_id"), report_id=report.id, owner_hash=owner_hash
    )
    allowed_keys = {item["model_key"] for item in models}
    if selected_key not in allowed_keys:
        selected_key = next((item["model_key"] for item in models if item["is_default"]), None)
        if selected_key is None and models:
            selected_key = models[0]["model_key"]
    return jsonify({"models": models, "selected_model_key": selected_key})


@bp.route("/api/chatbot/context/<slug>", methods=["GET"])
def report_context(slug):
    denied = _backoffice_required()
    if denied is not None:
        return denied
    try:
        info = get_workspace_info(slug)
    except DatasetResolutionError:
        logging.exception("[Chatbot] Dataset could not be resolved for context request")
        return jsonify({"error": "Dataset could not be resolved"}), 503
    if not info:
        return jsonify({"error": "Slug not found or inactive"}), 404
    return jsonify(info)


@bp.route("/api/chatbot/reports", methods=["GET"])
def active_reports():
    denied = _backoffice_required()
    if denied is not None:
        return denied
    return jsonify(get_all_active_reports())


@bp.route("/api/chatbot/diagnostics/<diagnostic_id>", methods=["GET"])
def diagnostic_lookup(diagnostic_id):
    """Resolve the opaque ``diagnostic_id`` shown to a client to the recorded turn.

    Backoffice only: it returns the effective identity (report, hashed dataset /
    workspace / credential) that is deliberately never sent to the browser.
    """
    denied = _backoffice_required()
    if denied is not None:
        return denied
    message = ChatMessage.query.filter_by(diagnostic_id=diagnostic_id).first()
    if message is None:
        return jsonify({"error": "Diagnostic not found"}), 404
    session = message.session
    return jsonify(
        {
            "diagnostic_id": message.diagnostic_id,
            "message_id": message.id,
            "session_id": message.session_id,
            "session_slug": session.slug if session else None,
            "session_report_id": session.report_id_fk if session else None,
            "report_id": message.report_id_fk,
            "dataset_hash": message.dataset_hash,
            "workspace_hash": message.workspace_hash,
            "credential_ref": message.credential_ref,
            "dataset_source": message.dataset_source,
            "trace_id": message.trace_id,
            "had_error": message.had_error,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }
    )


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
    denied = _backoffice_required()
    if denied is not None:
        return denied
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
                "last_model_key": s.last_model_key,
            }
            for s in sessions
        ]
    )


@bp.route("/api/chatbot/sessions/<int:session_id>", methods=["GET"])
def get_session(session_id):
    denied = _backoffice_required()
    if denied is not None:
        return denied
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
            "last_model_key": session.last_model_key,
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    "latency_ms": m.latency_ms,
                    "model_used": m.model_used,
                    "requested_model_key": m.requested_model_key,
                    "model_key": m.model_key,
                    "model_provider": m.model_provider,
                    "physical_model": m.physical_model,
                    "model_gateway": m.model_gateway,
                    "service_tier": m.service_tier,
                    "actual_model": m.actual_model,
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
