"""Regression tests for chat session / dataset isolation between reports and visitors.

Incident: a question asked from report 36 was answered with data/context of report 26.
Root causes covered here:

* a ``conversation_id`` was honoured without checking who owned the session or which
  report it belonged to, and the session was then re-labelled to the new report;
* WhatsApp kept the previous conversation when the contact switched reports;
* an unresolved dataset silently fell back to a process-wide ``CHATBOT_DATASET_ID``;
* internal chat transcripts / identifiers were readable without authentication.
"""
import asyncio
import os
import random
import re
import tempfile
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app import create_app, db
from app.models import (
    ChatMessage, ChatSession, Client, Permission, PublicLink, Report, Role, Tenant,
    User, UsuarioPBI, WhatsAppContact, Workspace,
)
from app.services import chatbot_service, session_owner
from app.services.analytics import AnalyticsResult
from app.utils import chatbot_context


def _fake_executor(log, *, delay=False):
    """Analytics executor double that records what each turn was given."""

    class Executor:
        async def prepare(self, request):
            if delay:
                await asyncio.sleep(random.random() * 0.01)
            return SimpleNamespace(report_id=request.report_id, question=request.question)

        async def execute_prepared(self, prepared, *, history=None, execution_id=None, trace_context=None):
            if delay:
                await asyncio.sleep(random.random() * 0.01)
            log.append({
                "report_id": prepared.report_id,
                "question": prepared.question,
                "history": list(history or []),
                "execution_id": execution_id,
            })
            return AnalyticsResult(
                answer=f"answer to {prepared.question} (report {prepared.report_id})",
                report_id=prepared.report_id,
            )

    return Executor()


class SeededAppCase(unittest.TestCase):
    """Two reports (26 and 36) of the same workspace, each behind its own public slug."""

    database_uri = "sqlite:///:memory:"

    def setUp(self):
        previous = os.environ.get("SQLALCHEMY_DATABASE_URI")
        os.environ["SQLALCHEMY_DATABASE_URI"] = self.database_uri
        try:
            self.app = create_app()
        finally:
            if previous is None:
                os.environ.pop("SQLALCHEMY_DATABASE_URI", None)
            else:
                os.environ["SQLALCHEMY_DATABASE_URI"] = previous
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            self._seed()
        self.calls = []

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _seed(self):
        client = Client(id=1, name="c", client_id="client-id")
        client.set_secret("s")
        db.session.add(client)
        db.session.flush()
        db.session.add(Tenant(id=1, name="t", tenant_id="tenant-id", client_id_fk=1))
        db.session.flush()
        db.session.add(Workspace(id=1, name="w", workspace_id="ws-id", tenant_id_fk=1))
        usuario = UsuarioPBI(id=1, nombre="u", username="u@example.com")
        usuario.set_password("p")
        db.session.add(usuario)
        db.session.flush()
        for report_id, slug in ((26, "slug26"), (36, "slug36")):
            db.session.add(Report(
                id=report_id, name=f"Report {report_id}", report_id=str(uuid.uuid4()),
                workspace_id_fk=1, usuario_pbi_id=1, es_publico=True, es_privado=False,
            ))
            db.session.flush()
            db.session.add(PublicLink(
                id=report_id, token=uuid.uuid4().hex[:16], custom_slug=slug,
                report_id_fk=report_id, is_active=True,
            ))
        backoffice = Permission(id=1, name="backoffice.access")
        role = Role(id=1, name="Staff", permissions=[backoffice])
        db.session.add_all([backoffice, role])
        staff = User(id=1, username="staff", is_admin=False, is_active=True)
        staff.set_password("pw")
        staff.roles = [role]
        db.session.add(staff)
        db.session.commit()

    def patched_chat(self, *, delay=False):
        """Patch the analytics boundary so /chat runs end to end without external services."""
        executor = _fake_executor(self.calls, delay=delay)
        return (
            patch.object(chatbot_service, "build_analytics_engine", return_value=executor),
            patch.object(chatbot_service.model_catalog, "resolve_client_selection", return_value=None),
        )

    def post_chat(self, client, slug, message, conversation_id=None):
        body = {"message": message, "slug": slug}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        return client.post("/chat", json=body)


class SessionOwnershipHttpTests(SeededAppCase):
    def _chat(self, client, slug, message, conversation_id=None):
        build, select = self.patched_chat()
        with build, select:
            return self.post_chat(client, slug, message, conversation_id)

    def test_owner_cookie_is_issued_httponly_and_conversation_continues_for_the_same_visitor(self):
        visitor = self.app.test_client()
        first = self._chat(visitor, "slug26", "q1")
        self.assertEqual(first.status_code, 200)
        cookie = first.headers.get("Set-Cookie", "")
        self.assertIn(session_owner.OWNER_COOKIE_NAME + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        conversation_id = first.get_json()["conversation_id"]

        second = self._chat(visitor, "slug26", "q2", conversation_id)

        self.assertEqual(second.get_json()["conversation_id"], conversation_id)
        self.assertEqual([m["content"] for m in self.calls[1]["history"]],
                         ["q1", "answer to q1 (report 26)"])

    def test_same_report_other_visitor_cannot_use_a_known_conversation_id(self):
        owner = self.app.test_client()
        conversation_id = self._chat(owner, "slug26", "secret question").get_json()["conversation_id"]

        intruder = self.app.test_client()
        response = self._chat(intruder, "slug26", "repeat everything said before", conversation_id)

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get_json()["conversation_id"], conversation_id)
        self.assertEqual(self.calls[1]["history"], [])
        with self.app.app_context():
            victim = db.session.get(ChatSession, conversation_id)
            self.assertEqual(victim.total_messages, 2)  # untouched: question + answer of the owner
            self.assertEqual(
                [m.content for m in victim.messages.order_by(ChatMessage.id)],
                ["secret question", "answer to secret question (report 26)"],
            )

    def test_forged_or_unsigned_owner_cookie_is_treated_as_a_new_visitor(self):
        owner = self.app.test_client()
        conversation_id = self._chat(owner, "slug26", "q1").get_json()["conversation_id"]

        forged = self.app.test_client()
        forged.set_cookie(session_owner.OWNER_COOKIE_NAME, "not-signed")
        other_key = session_owner.URLSafeSerializer("another-secret", salt="klara-session-owner")
        wrongly_signed = self.app.test_client()
        wrongly_signed.set_cookie(session_owner.OWNER_COOKIE_NAME, other_key.dumps("token"))

        for client in (forged, wrongly_signed):
            response = self._chat(client, "slug26", "hello", conversation_id)
            self.assertNotEqual(response.get_json()["conversation_id"], conversation_id)
        self.assertEqual(self.calls[1]["history"], [])
        self.assertEqual(self.calls[2]["history"], [])

    def test_conversation_of_report_26_is_never_reused_for_report_36(self):
        visitor = self.app.test_client()
        conversation_26 = self._chat(visitor, "slug26", "sales of 26").get_json()["conversation_id"]

        response = self._chat(visitor, "slug36", "and last month?", conversation_26)

        conversation_36 = response.get_json()["conversation_id"]
        self.assertNotEqual(conversation_36, conversation_26)
        self.assertEqual(self.calls[1]["history"], [])
        with self.app.app_context():
            original = db.session.get(ChatSession, conversation_26)
            self.assertEqual((original.report_id_fk, original.slug), (26, "slug26"))
            self.assertEqual(original.total_messages, 2)
            created = db.session.get(ChatSession, conversation_36)
            self.assertEqual((created.report_id_fk, created.slug), (36, "slug36"))

    def test_session_identity_is_never_rewritten(self):
        visitor = self.app.test_client()
        conversation_id = self._chat(visitor, "slug26", "q1").get_json()["conversation_id"]
        self._chat(visitor, "slug26", "q2", conversation_id)
        self._chat(visitor, "slug36", "q3", conversation_id)

        with self.app.app_context():
            session = db.session.get(ChatSession, conversation_id)
            self.assertEqual((session.report_id_fk, session.slug), (26, "slug26"))

    def test_legacy_sessions_without_owner_are_not_reused(self):
        with self.app.app_context():
            db.session.add(ChatSession(id=500, slug="slug26", report_id_fk=26, title="legacy"))
            db.session.add(ChatMessage(id=900, session_id=500, role="user", content="old"))
            db.session.commit()

        response = self._chat(self.app.test_client(), "slug26", "q", "500")

        self.assertNotEqual(response.get_json()["conversation_id"], 500)
        self.assertEqual(self.calls[0]["history"], [])

    def test_logged_in_user_owns_the_session_across_browsers(self):
        first = self.app.test_client()
        second = self.app.test_client()
        for client in (first, second):
            client.post("/login", data={"username": "staff", "password": "pw"})
        conversation_id = self._chat(first, "slug26", "q1").get_json()["conversation_id"]

        again = self._chat(second, "slug26", "q2", conversation_id)

        self.assertEqual(again.get_json()["conversation_id"], conversation_id)

    def test_models_endpoint_does_not_leak_the_model_of_a_foreign_session(self):
        with self.app.app_context():
            db.session.add(ChatSession(
                id=700, slug="slug26", report_id_fk=26, owner_hash=session_owner.owner_hash("visitor", "x"),
                last_model_key="private-model",
            ))
            db.session.commit()
        models = [{"model_key": "default", "is_default": True}, {"model_key": "private-model", "is_default": False}]
        with patch("app.routes.chatbot.model_catalog.available_client_models", return_value=models):
            response = self.app.test_client().get("/api/chatbot/models?slug=slug26&conversation_id=700")

        self.assertEqual(response.get_json()["selected_model_key"], "default")


class ConcurrentIsolationTests(SeededAppCase):
    """Simultaneous requests for both reports never mix sessions, histories or report ids."""

    def setUp(self):
        self.db_dir = tempfile.TemporaryDirectory()
        self.database_uri = "sqlite:///" + os.path.join(self.db_dir.name, "isolation.db").replace("\\", "/")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        with self.app.app_context():
            db.engine.dispose()
        self.db_dir.cleanup()

    def test_parallel_requests_keep_report_session_and_history_separate(self):
        threads_count, turns = 8, 3
        build, select = self.patched_chat(delay=True)
        results, errors = {}, []
        # Visitor k talks to report 26 (even k) or 36 (odd k); each also tries to
        # reuse the conversation of the previous visitor on the *other* report.
        first_ids = {}
        barrier = threading.Barrier(threads_count)

        def visitor(k):
            slug = "slug26" if k % 2 == 0 else "slug36"
            other = "slug36" if k % 2 == 0 else "slug26"
            client = self.app.test_client()
            try:
                barrier.wait(timeout=30)
                conversation_id = None
                for turn in range(turns):
                    response = self.post_chat(client, slug, f"v{k}-t{turn}", conversation_id)
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    conversation_id = response.get_json()["conversation_id"]
                    first_ids.setdefault(k, conversation_id)
                    # cross attempt: same conversation id against the other report
                    crossed = self.post_chat(client, other, f"v{k}-cross{turn}", conversation_id)
                    self.assertEqual(crossed.status_code, 200)
                    self.assertNotEqual(crossed.get_json()["conversation_id"], conversation_id)
                results[k] = conversation_id
            except Exception as exc:  # pragma: no cover - reported below
                errors.append((k, repr(exc)))

        # SQLite has no autoincrement for these BigInteger keys, so the service
        # allocates ids as max(id)+1, which races between threads. That is an
        # SQLite-only artefact (PostgreSQL uses sequences); serialise allocation
        # here so the test exercises isolation rather than that race.
        id_lock, counters = threading.Lock(), {}

        def thread_safe_sqlite_id(instance, model):
            if getattr(instance, "id", None) is None:
                with id_lock:
                    counters[model] = counters.get(model, 10_000) + 1
                    instance.id = counters[model]

        with build, select, patch.object(chatbot_service, "_prepare_sqlite_id", thread_safe_sqlite_id):
            workers = [threading.Thread(target=visitor, args=(k,)) for k in range(threads_count)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=120)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), threads_count)
        with self.app.app_context():
            for k, conversation_id in results.items():
                expected_report = 26 if k % 2 == 0 else 36
                session = db.session.get(ChatSession, conversation_id)
                self.assertEqual(session.report_id_fk, expected_report)
                contents = [m.content for m in session.messages.order_by(ChatMessage.id) if m.role == "user"]
                self.assertEqual(contents, [f"v{k}-t{turn}" for turn in range(turns)])
            # No session contains messages written by two different visitors or reports.
            for session in ChatSession.query.all():
                owners = {m.content.split("-")[0] for m in session.messages if m.role == "user"}
                self.assertLessEqual(len(owners), 1, f"session {session.id} mixes visitors: {owners}")
                for m in session.messages:
                    if m.role == "assistant":
                        self.assertIn(f"(report {session.report_id_fk})", m.content)
        for call in self.calls:
            visitor_tag = call["question"].split("-")[0]
            for entry in call["history"]:
                if entry["role"] == "user":
                    self.assertTrue(entry["content"].startswith(visitor_tag), (call, entry))


class WhatsAppIsolationTests(SeededAppCase):
    def _contact(self, **kwargs):
        contact = WhatsAppContact(phone_number="5493625000000", **kwargs)
        db.session.add(contact)
        db.session.commit()
        return contact.id

    def test_switching_report_clears_the_previous_conversation(self):
        from app.routes import whatsapp

        with self.app.app_context():
            db.session.add(ChatSession(id=11, slug="slug26", report_id_fk=26))
            db.session.commit()
            contact_id = self._contact(report_id_fk=26, conversation_id=11)

            whatsapp._select_report_sync(contact_id, 36)

            contact = db.session.get(WhatsAppContact, contact_id)
            self.assertEqual(contact.report_id_fk, 36)
            self.assertIsNone(contact.conversation_id)

    def test_reselecting_the_same_report_keeps_the_conversation(self):
        from app.routes import whatsapp

        with self.app.app_context():
            db.session.add(ChatSession(id=12, slug="slug26", report_id_fk=26))
            db.session.commit()
            contact_id = self._contact(report_id_fk=26, conversation_id=12)

            whatsapp._select_report_sync(contact_id, 26)

            self.assertEqual(db.session.get(WhatsAppContact, contact_id).conversation_id, 12)

    def test_returning_to_the_menu_clears_the_conversation(self):
        from app.routes import whatsapp

        with self.app.app_context():
            db.session.add(ChatSession(id=13, slug="slug26", report_id_fk=26))
            db.session.commit()
            contact_id = self._contact(report_id_fk=26, conversation_id=13)

            whatsapp._reset_to_menu_sync(contact_id)

            contact = db.session.get(WhatsAppContact, contact_id)
            self.assertIsNone(contact.report_id_fk)
            self.assertIsNone(contact.conversation_id)

    def test_whatsapp_session_is_bound_to_the_number_and_never_crosses_reports(self):
        owner = session_owner.whatsapp_owner_hash("5493625000000")
        other = session_owner.whatsapp_owner_hash("5493625999999")
        build, select = self.patched_chat()
        with self.app.app_context(), build, select:
            first = asyncio.run(chatbot_service.procesar_interaccion_completa(
                "q26", slug="slug26", user_key="whatsapp:x", source="whatsapp", config={}, owner_hash=owner,
            ))
            crossed_report = asyncio.run(chatbot_service.procesar_interaccion_completa(
                "q36", slug="slug36", user_key="whatsapp:x", source="whatsapp", config={}, owner_hash=owner,
                conversation_id=str(first["conversation_id"]),
            ))
            other_number = asyncio.run(chatbot_service.procesar_interaccion_completa(
                "hello", slug="slug26", user_key="whatsapp:y", source="whatsapp", config={}, owner_hash=other,
                conversation_id=str(first["conversation_id"]),
            ))

        self.assertNotEqual(crossed_report["conversation_id"], first["conversation_id"])
        self.assertNotEqual(other_number["conversation_id"], first["conversation_id"])
        self.assertEqual(self.calls[1]["history"], [])
        self.assertEqual(self.calls[2]["history"], [])


class WebhookSignatureTests(unittest.TestCase):
    def test_unsigned_webhooks_are_rejected_without_an_app_secret(self):
        from app.routes import whatsapp

        with patch.dict(os.environ, {"META_WA_APP_SECRET": "", "META_WA_ALLOW_UNSIGNED": ""}):
            self.assertFalse(whatsapp._verify_meta_signature(b"{}", ""))
            self.assertFalse(whatsapp._verify_meta_signature(b"{}", "sha256=whatever"))

    def test_unsigned_webhooks_need_an_explicit_opt_in(self):
        from app.routes import whatsapp

        with patch.dict(os.environ, {"META_WA_APP_SECRET": "", "META_WA_ALLOW_UNSIGNED": "true"}):
            self.assertTrue(whatsapp._verify_meta_signature(b"{}", ""))

    def test_valid_and_invalid_signatures_with_secret(self):
        import hashlib
        import hmac

        from app.routes import whatsapp

        body = b'{"entry": []}'
        good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"META_WA_APP_SECRET": "secret", "META_WA_ALLOW_UNSIGNED": "true"}):
            self.assertTrue(whatsapp._verify_meta_signature(body, good))
            self.assertFalse(whatsapp._verify_meta_signature(body, "sha256=bad"))
            self.assertFalse(whatsapp._verify_meta_signature(body, ""))


class DatasetResolutionTests(SeededAppCase):
    def test_context_helpers_never_substitute_an_unresolved_dataset(self):
        with self.app.app_context(), \
                patch.dict(os.environ, {"CHATBOT_DATASET_ID": "dataset-of-report-26"}), \
                patch("app.utils.chatbot_context.get_current_dataset_id", side_effect=RuntimeError("azure down")):
            for call in (
                lambda: chatbot_context.get_workspace_info("slug36"),
                lambda: chatbot_context.get_report_and_dataset_by_slug("slug36"),
                lambda: chatbot_context.get_report_context("slug36"),
            ):
                with self.assertRaises(chatbot_context.DatasetResolutionError):
                    call()

    def test_resolved_dataset_is_the_one_of_the_requested_report(self):
        datasets = {26: "ds-26", 36: "ds-36"}
        with self.app.app_context(), \
                patch("app.utils.chatbot_context.get_current_dataset_id", side_effect=lambda r: datasets[r.id]):
            self.assertEqual(chatbot_context.get_workspace_info("slug26")["dataset_id"], "ds-26")
            self.assertEqual(chatbot_context.get_workspace_info("slug36")["dataset_id"], "ds-36")
            report, dataset_id = chatbot_context.get_report_and_dataset_by_slug("slug36")
            self.assertEqual((report.id, dataset_id), (36, "ds-36"))

    def test_chat_service_does_not_use_a_process_wide_dataset(self):
        """The chat path must not read CHATBOT_DATASET_ID anywhere."""
        import pathlib

        offenders = [
            str(path) for path in pathlib.Path("app").rglob("*.py")
            if re.search(r"(getenv|environ)[^\n]*CHATBOT_DATASET_ID", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


class SessionOwnerPrimitivesTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "unit-secret"

    def test_owner_hash_is_namespaced_stable_and_not_reversible(self):
        with self.app.app_context():
            visitor = session_owner.owner_hash("visitor", "42")
            user = session_owner.owner_hash("user", "42")
            self.assertEqual(visitor, session_owner.owner_hash("visitor", "42"))
            self.assertNotEqual(visitor, user)
            self.assertEqual(len(visitor), 64)
            self.assertNotIn("visitor-token-abcdef", session_owner.owner_hash("visitor", "visitor-token-abcdef"))
            self.assertIsNone(session_owner.owner_hash("visitor", ""))

    def test_hash_depends_on_the_server_secret(self):
        with self.app.app_context():
            first = session_owner.owner_hash("visitor", "42")
        self.app.config["SECRET_KEY"] = "another-secret"
        with self.app.app_context():
            self.assertNotEqual(first, session_owner.owner_hash("visitor", "42"))

    def test_cookie_round_trip_and_tampering(self):
        with self.app.app_context():
            token, signed = session_owner.issue_visitor_token()
            self.assertEqual(session_owner.read_visitor_token(signed), token)
            self.assertIsNone(session_owner.read_visitor_token(signed + "x"))
            self.assertIsNone(session_owner.read_visitor_token(token))
            self.assertIsNone(session_owner.read_visitor_token(""))
            self.assertIsNone(session_owner.read_visitor_token(None))

    def test_missing_owner_never_matches(self):
        self.assertFalse(session_owner.owners_match(None, None))
        self.assertFalse(session_owner.owners_match("a", None))
        self.assertFalse(session_owner.owners_match(None, "a"))
        self.assertTrue(session_owner.owners_match("a", "a"))


class InternalEndpointsAreNotPublicTests(SeededAppCase):
    def test_transcripts_and_inventories_require_backoffice(self):
        with self.app.app_context():
            db.session.add(ChatSession(id=800, slug="slug26", report_id_fk=26, title="t"))
            db.session.commit()
        anonymous = self.app.test_client()
        for url in ("/api/chatbot/sessions", "/api/chatbot/sessions/800",
                    "/api/chatbot/reports", "/api/chatbot/context/slug26"):
            with self.subTest(url):
                self.assertIn(anonymous.get(url).status_code, (401, 403))

        staff = self.app.test_client()
        staff.post("/login", data={"username": "staff", "password": "pw"})
        for url in ("/api/chatbot/sessions", "/api/chatbot/sessions/800", "/api/chatbot/reports"):
            with self.subTest(url):
                self.assertEqual(staff.get(url).status_code, 200)


if __name__ == "__main__":
    unittest.main()
