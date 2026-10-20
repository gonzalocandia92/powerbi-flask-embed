"""add report_reviews and report_review_events (review/approval of report PDFs)

Additive. Creates the two review tables, a unique index on report_run_artifacts(id, report_run_id)
(required so a review can reference the PAIR artifact+run with a composite foreign key) and the
immutability triggers (PostgreSQL and SQLite). The SQL of the triggers below is a FROZEN copy: do
not edit it when the rules change, add a new migration that re-creates them.

Idempotent: tables may already exist (db.create_all()); the triggers are always re-created.

Revision ID: 7b3c4d5e6f80
Revises: 6a2b3c4d5e7f
Create Date: 2026-10-16 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '7b3c4d5e6f80'
down_revision = '6a2b3c4d5e7f'
branch_labels = None
depends_on = None

ARTIFACT_INDEX = 'uq_report_run_artifact_id_run'

_TRIGGERS_UP = {
    'postgresql': [
        """CREATE OR REPLACE FUNCTION fn_report_reviews_guard_update() RETURNS trigger AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.report_run_id IS DISTINCT FROM OLD.report_run_id OR NEW.round IS DISTINCT FROM OLD.round OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN RAISE EXCEPTION 'report_reviews: identity columns are immutable' USING ERRCODE = '23000'; END IF;
  IF (OLD.pdf_artifact_id IS NOT NULL AND NEW.pdf_artifact_id IS DISTINCT FROM OLD.pdf_artifact_id) OR (OLD.pdf_sha256 IS NOT NULL AND NEW.pdf_sha256 IS DISTINCT FROM OLD.pdf_sha256) OR (OLD.final_report_artifact_id IS NOT NULL AND NEW.final_report_artifact_id IS DISTINCT FROM OLD.final_report_artifact_id) OR (OLD.html_artifact_id IS NOT NULL AND NEW.html_artifact_id IS DISTINCT FROM OLD.html_artifact_id) THEN RAISE EXCEPTION 'report_reviews: a pinned artifact reference is immutable' USING ERRCODE = '23000'; END IF;
  IF (OLD.submitted_by_user_id IS NOT NULL AND (NEW.submitted_by_user_id IS DISTINCT FROM OLD.submitted_by_user_id AND NEW.submitted_by_user_id IS NOT NULL)) OR (OLD.decided_by_user_id IS NOT NULL AND (NEW.decided_by_user_id IS DISTINCT FROM OLD.decided_by_user_id AND NEW.decided_by_user_id IS NOT NULL)) THEN RAISE EXCEPTION 'report_reviews: an actor cannot be reassigned' USING ERRCODE = '23000'; END IF;
  IF NEW.status <> OLD.status AND NOT ((OLD.status = 'draft' AND NEW.status IN ('pending_review')) OR (OLD.status = 'pending_review' AND NEW.status IN ('approved', 'rejected'))) THEN RAISE EXCEPTION 'report_reviews: status transition not allowed' USING ERRCODE = '23000'; END IF;
  IF OLD.status IN ('approved', 'rejected') AND ((NEW.id IS DISTINCT FROM OLD.id OR NEW.report_run_id IS DISTINCT FROM OLD.report_run_id OR NEW.round IS DISTINCT FROM OLD.round OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.pdf_artifact_id IS DISTINCT FROM OLD.pdf_artifact_id OR NEW.pdf_sha256 IS DISTINCT FROM OLD.pdf_sha256 OR NEW.final_report_artifact_id IS DISTINCT FROM OLD.final_report_artifact_id OR NEW.html_artifact_id IS DISTINCT FROM OLD.html_artifact_id OR NEW.status IS DISTINCT FROM OLD.status OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at OR NEW.decided_at IS DISTINCT FROM OLD.decided_at OR NEW.rejection_reason IS DISTINCT FROM OLD.rejection_reason OR NEW.updated_at IS DISTINCT FROM OLD.updated_at) OR (NEW.submitted_by_user_id IS DISTINCT FROM OLD.submitted_by_user_id AND NEW.submitted_by_user_id IS NOT NULL) OR (NEW.decided_by_user_id IS DISTINCT FROM OLD.decided_by_user_id AND NEW.decided_by_user_id IS NOT NULL)) THEN RAISE EXCEPTION 'report_reviews: a decided review is immutable' USING ERRCODE = '23000'; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql""",
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_update ON report_reviews""",
        """CREATE TRIGGER trg_report_reviews_guard_update BEFORE UPDATE ON report_reviews FOR EACH ROW EXECUTE FUNCTION fn_report_reviews_guard_update()""",
        """CREATE OR REPLACE FUNCTION fn_report_reviews_guard_delete() RETURNS trigger AS $$
BEGIN
  IF OLD.status <> 'draft' THEN RAISE EXCEPTION 'report_reviews: only a draft can be deleted' USING ERRCODE = '23000'; END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql""",
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_delete ON report_reviews""",
        """CREATE TRIGGER trg_report_reviews_guard_delete BEFORE DELETE ON report_reviews FOR EACH ROW EXECUTE FUNCTION fn_report_reviews_guard_delete()""",
        """CREATE OR REPLACE FUNCTION fn_report_review_events_guard_update() RETURNS trigger AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.review_id IS DISTINCT FROM OLD.review_id OR NEW.event_type IS DISTINCT FROM OLD.event_type OR NEW.previous_status IS DISTINCT FROM OLD.previous_status OR NEW.new_status IS DISTINCT FROM OLD.new_status OR NEW.comment IS DISTINCT FROM OLD.comment OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.metadata_json::text IS DISTINCT FROM OLD.metadata_json::text OR (NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id AND NEW.actor_user_id IS NOT NULL) THEN RAISE EXCEPTION 'report_review_events: append-only, rows cannot be modified' USING ERRCODE = '23000'; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_update ON report_review_events""",
        """CREATE TRIGGER trg_report_review_events_guard_update BEFORE UPDATE ON report_review_events FOR EACH ROW EXECUTE FUNCTION fn_report_review_events_guard_update()""",
        """CREATE OR REPLACE FUNCTION fn_report_review_events_guard_delete() RETURNS trigger AS $$
BEGIN
  IF TRUE THEN RAISE EXCEPTION 'report_review_events: append-only, rows cannot be deleted' USING ERRCODE = '23000'; END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_delete ON report_review_events""",
        """CREATE TRIGGER trg_report_review_events_guard_delete BEFORE DELETE ON report_review_events FOR EACH ROW EXECUTE FUNCTION fn_report_review_events_guard_delete()""",
        """CREATE OR REPLACE FUNCTION fn_report_run_artifacts_guard_update() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM report_reviews r WHERE r.pdf_artifact_id = OLD.id OR r.html_artifact_id = OLD.id OR r.final_report_artifact_id = OLD.id) THEN RAISE EXCEPTION 'report_run_artifacts: an artifact under review is immutable' USING ERRCODE = '23000'; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_update ON report_run_artifacts""",
        """CREATE TRIGGER trg_report_run_artifacts_guard_update BEFORE UPDATE ON report_run_artifacts FOR EACH ROW EXECUTE FUNCTION fn_report_run_artifacts_guard_update()""",
        """CREATE OR REPLACE FUNCTION fn_report_run_artifacts_guard_delete() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM report_reviews r WHERE r.pdf_artifact_id = OLD.id OR r.html_artifact_id = OLD.id OR r.final_report_artifact_id = OLD.id) THEN RAISE EXCEPTION 'report_run_artifacts: an artifact under review cannot be deleted' USING ERRCODE = '23000'; END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_delete ON report_run_artifacts""",
        """CREATE TRIGGER trg_report_run_artifacts_guard_delete BEFORE DELETE ON report_run_artifacts FOR EACH ROW EXECUTE FUNCTION fn_report_run_artifacts_guard_delete()""",
    ],
    'sqlite': [
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_update""",
        """CREATE TRIGGER trg_report_reviews_guard_update BEFORE UPDATE ON report_reviews FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.id IS NOT OLD.id OR NEW.report_run_id IS NOT OLD.report_run_id OR NEW.round IS NOT OLD.round OR NEW.created_at IS NOT OLD.created_at) THEN RAISE(ABORT, 'report_reviews: identity columns are immutable') END;
  SELECT CASE WHEN ((OLD.pdf_artifact_id IS NOT NULL AND NEW.pdf_artifact_id IS NOT OLD.pdf_artifact_id) OR (OLD.pdf_sha256 IS NOT NULL AND NEW.pdf_sha256 IS NOT OLD.pdf_sha256) OR (OLD.final_report_artifact_id IS NOT NULL AND NEW.final_report_artifact_id IS NOT OLD.final_report_artifact_id) OR (OLD.html_artifact_id IS NOT NULL AND NEW.html_artifact_id IS NOT OLD.html_artifact_id)) THEN RAISE(ABORT, 'report_reviews: a pinned artifact reference is immutable') END;
  SELECT CASE WHEN ((OLD.submitted_by_user_id IS NOT NULL AND (NEW.submitted_by_user_id IS NOT OLD.submitted_by_user_id AND NEW.submitted_by_user_id IS NOT NULL)) OR (OLD.decided_by_user_id IS NOT NULL AND (NEW.decided_by_user_id IS NOT OLD.decided_by_user_id AND NEW.decided_by_user_id IS NOT NULL))) THEN RAISE(ABORT, 'report_reviews: an actor cannot be reassigned') END;
  SELECT CASE WHEN (NEW.status <> OLD.status AND NOT ((OLD.status = 'draft' AND NEW.status IN ('pending_review')) OR (OLD.status = 'pending_review' AND NEW.status IN ('approved', 'rejected')))) THEN RAISE(ABORT, 'report_reviews: status transition not allowed') END;
  SELECT CASE WHEN (OLD.status IN ('approved', 'rejected') AND ((NEW.id IS NOT OLD.id OR NEW.report_run_id IS NOT OLD.report_run_id OR NEW.round IS NOT OLD.round OR NEW.created_at IS NOT OLD.created_at OR NEW.pdf_artifact_id IS NOT OLD.pdf_artifact_id OR NEW.pdf_sha256 IS NOT OLD.pdf_sha256 OR NEW.final_report_artifact_id IS NOT OLD.final_report_artifact_id OR NEW.html_artifact_id IS NOT OLD.html_artifact_id OR NEW.status IS NOT OLD.status OR NEW.submitted_at IS NOT OLD.submitted_at OR NEW.decided_at IS NOT OLD.decided_at OR NEW.rejection_reason IS NOT OLD.rejection_reason OR NEW.updated_at IS NOT OLD.updated_at) OR (NEW.submitted_by_user_id IS NOT OLD.submitted_by_user_id AND NEW.submitted_by_user_id IS NOT NULL) OR (NEW.decided_by_user_id IS NOT OLD.decided_by_user_id AND NEW.decided_by_user_id IS NOT NULL))) THEN RAISE(ABORT, 'report_reviews: a decided review is immutable') END;
END""",
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_delete""",
        """CREATE TRIGGER trg_report_reviews_guard_delete BEFORE DELETE ON report_reviews FOR EACH ROW
BEGIN
  SELECT CASE WHEN (OLD.status <> 'draft') THEN RAISE(ABORT, 'report_reviews: only a draft can be deleted') END;
END""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_update""",
        """CREATE TRIGGER trg_report_review_events_guard_update BEFORE UPDATE ON report_review_events FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.id IS NOT OLD.id OR NEW.review_id IS NOT OLD.review_id OR NEW.event_type IS NOT OLD.event_type OR NEW.previous_status IS NOT OLD.previous_status OR NEW.new_status IS NOT OLD.new_status OR NEW.comment IS NOT OLD.comment OR NEW.created_at IS NOT OLD.created_at OR NEW.metadata_json IS NOT OLD.metadata_json OR (NEW.actor_user_id IS NOT OLD.actor_user_id AND NEW.actor_user_id IS NOT NULL)) THEN RAISE(ABORT, 'report_review_events: append-only, rows cannot be modified') END;
END""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_delete""",
        """CREATE TRIGGER trg_report_review_events_guard_delete BEFORE DELETE ON report_review_events FOR EACH ROW
BEGIN
  SELECT CASE WHEN (TRUE) THEN RAISE(ABORT, 'report_review_events: append-only, rows cannot be deleted') END;
END""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_update""",
        """CREATE TRIGGER trg_report_run_artifacts_guard_update BEFORE UPDATE ON report_run_artifacts FOR EACH ROW
BEGIN
  SELECT CASE WHEN (EXISTS (SELECT 1 FROM report_reviews r WHERE r.pdf_artifact_id = OLD.id OR r.html_artifact_id = OLD.id OR r.final_report_artifact_id = OLD.id)) THEN RAISE(ABORT, 'report_run_artifacts: an artifact under review is immutable') END;
END""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_delete""",
        """CREATE TRIGGER trg_report_run_artifacts_guard_delete BEFORE DELETE ON report_run_artifacts FOR EACH ROW
BEGIN
  SELECT CASE WHEN (EXISTS (SELECT 1 FROM report_reviews r WHERE r.pdf_artifact_id = OLD.id OR r.html_artifact_id = OLD.id OR r.final_report_artifact_id = OLD.id)) THEN RAISE(ABORT, 'report_run_artifacts: an artifact under review cannot be deleted') END;
END""",
    ],
}

_TRIGGERS_DOWN = {
    'postgresql': [
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_update ON report_reviews""",
        """DROP FUNCTION IF EXISTS fn_report_reviews_guard_update()""",
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_delete ON report_reviews""",
        """DROP FUNCTION IF EXISTS fn_report_reviews_guard_delete()""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_update ON report_review_events""",
        """DROP FUNCTION IF EXISTS fn_report_review_events_guard_update()""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_delete ON report_review_events""",
        """DROP FUNCTION IF EXISTS fn_report_review_events_guard_delete()""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_update ON report_run_artifacts""",
        """DROP FUNCTION IF EXISTS fn_report_run_artifacts_guard_update()""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_delete ON report_run_artifacts""",
        """DROP FUNCTION IF EXISTS fn_report_run_artifacts_guard_delete()""",
    ],
    'sqlite': [
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_update""",
        """DROP TRIGGER IF EXISTS trg_report_reviews_guard_delete""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_update""",
        """DROP TRIGGER IF EXISTS trg_report_review_events_guard_delete""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_update""",
        """DROP TRIGGER IF EXISTS trg_report_run_artifacts_guard_delete""",
    ],
}


def _big_id():
    return sa.BigInteger().with_variant(sa.Integer, 'sqlite')


def _composite_fk(column, name):
    return sa.ForeignKeyConstraint(
        [column, 'report_run_id'], ['report_run_artifacts.id', 'report_run_artifacts.report_run_id'],
        ondelete='RESTRICT', name=name)


def _run(statements):
    for statement in statements:
        op.get_bind().exec_driver_sql(statement)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('report_run_artifacts') and ARTIFACT_INDEX not in {
            index['name'] for index in inspector.get_indexes('report_run_artifacts')}:
        op.create_index(ARTIFACT_INDEX, 'report_run_artifacts', ['id', 'report_run_id'], unique=True)

    if not inspector.has_table('report_reviews'):
        op.create_table(
            'report_reviews',
            sa.Column('id', _big_id(), primary_key=True, autoincrement=True),
            sa.Column('report_run_id', sa.String(length=36), nullable=False),
            sa.Column('round', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='draft'),
            sa.Column('pdf_artifact_id', _big_id(), nullable=True),
            sa.Column('pdf_sha256', sa.String(length=64), nullable=True),
            sa.Column('final_report_artifact_id', _big_id(), nullable=True),
            sa.Column('html_artifact_id', _big_id(), nullable=True),
            sa.Column('submitted_by_user_id', sa.BigInteger(), nullable=True),
            sa.Column('submitted_at', sa.DateTime(), nullable=True),
            sa.Column('decided_by_user_id', sa.BigInteger(), nullable=True),
            sa.Column('decided_at', sa.DateTime(), nullable=True),
            sa.Column('rejection_reason', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['report_run_id'], ['report_runs.id'], ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['submitted_by_user_id'], ['users.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['decided_by_user_id'], ['users.id'], ondelete='SET NULL'),
            _composite_fk('pdf_artifact_id', 'fk_report_review_pdf'),
            _composite_fk('html_artifact_id', 'fk_report_review_html'),
            _composite_fk('final_report_artifact_id', 'fk_report_review_final_report'),
            sa.UniqueConstraint('report_run_id', 'round', name='uq_report_review_run_round'),
            sa.CheckConstraint("status IN ('draft', 'pending_review', 'approved', 'rejected')",
                               name='ck_report_review_status'),
            sa.CheckConstraint("status = 'draft' OR (pdf_artifact_id IS NOT NULL AND pdf_sha256 IS NOT NULL)",
                               name='ck_report_review_pdf_when_submitted'),
            sa.CheckConstraint("status = 'draft' OR submitted_at IS NOT NULL", name='ck_report_review_submitted_at'),
            sa.CheckConstraint("NOT (status IN ('approved', 'rejected')) OR decided_at IS NOT NULL",
                               name='ck_report_review_decided_at'),
            sa.CheckConstraint(
                "status <> 'rejected' OR (rejection_reason IS NOT NULL AND length(trim(rejection_reason)) > 0)",
                name='ck_report_review_rejection_reason'),
        )
        op.create_index('ix_report_reviews_submitted_by_user_id', 'report_reviews', ['submitted_by_user_id'])
        op.create_index('ix_report_reviews_decided_by_user_id', 'report_reviews', ['decided_by_user_id'])
        op.create_index('ix_report_reviews_status_submitted', 'report_reviews', ['status', 'submitted_at'])

    if not inspector.has_table('report_review_events'):
        op.create_table(
            'report_review_events',
            sa.Column('id', _big_id(), primary_key=True, autoincrement=True),
            sa.Column('review_id', _big_id(), nullable=False),
            sa.Column('actor_user_id', sa.BigInteger(), nullable=True),
            sa.Column('event_type', sa.String(length=40), nullable=False),
            sa.Column('previous_status', sa.String(length=20), nullable=True),
            sa.Column('new_status', sa.String(length=20), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['review_id'], ['report_reviews.id'], ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_report_review_events_actor_user_id', 'report_review_events', ['actor_user_id'])
        op.create_index('ix_report_review_events_review', 'report_review_events', ['review_id', 'id'])

    _run(_TRIGGERS_UP.get(bind.dialect.name, []))


def downgrade():
    bind = op.get_bind()
    _run(_TRIGGERS_DOWN.get(bind.dialect.name, []))
    inspector = sa.inspect(bind)
    if inspector.has_table('report_review_events'):
        op.drop_table('report_review_events')
    if inspector.has_table('report_reviews'):
        op.drop_table('report_reviews')
    if inspector.has_table('report_run_artifacts') and ARTIFACT_INDEX in {
            index['name'] for index in inspector.get_indexes('report_run_artifacts')}:
        op.drop_index(ARTIFACT_INDEX, table_name='report_run_artifacts')
