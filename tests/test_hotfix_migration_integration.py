"""Validate the hotfix on top of the existing KLARA history, never a real database."""
import importlib.util
from pathlib import Path
import unittest

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

VERSIONS = Path(__file__).resolve().parents[1] / 'migrations' / 'versions'
FILES = (
    '91af0b7c2d10_add_public_link_reset_to_default.py',
    '91af0b7c2d11_move_link_actions_to_report.py',
    '91af0b7c2d12_add_report_requires_login.py',
)


def load(filename):
    spec = importlib.util.spec_from_file_location(filename[:-3], VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HotfixMigrationIntegrationTest(unittest.TestCase):
    def test_one_head_and_incremental_path_after_existing_klara_head(self):
        scripts = ScriptDirectory(str(VERSIONS.parent))
        self.assertEqual(scripts.get_heads(), ['91af0b7c2d12'])
        path = list(scripts.iterate_revisions('heads', 'b8c9d0e1f2a3'))
        self.assertEqual([r.revision for r in reversed(path)],
                         ['91af0b7c2d10', '91af0b7c2d11', '91af0b7c2d12'])
        baseline = scripts.get_revision('ebcd339309df')
        self.assertEqual(baseline.down_revision, 'c2d3e4f5a6b7')
        self.assertTrue(baseline.path.endswith('ebcd339309df_add_klara_catalog_baseline.py'))

    def test_upgrade_and_downgrade_preserve_klara_data_and_original_schema(self):
        self._check_roundtrip(preexisting_reset=False)

    def test_upgrade_preserves_and_moves_preexisting_reset_values(self):
        self._check_roundtrip(preexisting_reset=True)

    def _check_roundtrip(self, *, preexisting_reset):
        engine = sa.create_engine('sqlite://')
        migrations = [load(f) for f in FILES]
        with engine.begin() as connection:
            statements = (
                'CREATE TABLE reports (id INTEGER PRIMARY KEY, name TEXT NOT NULL)',
                'CREATE TABLE public_links (id INTEGER PRIMARY KEY, report_id_fk INTEGER, '
                'is_active BOOLEAN, allow_refresh BOOLEAN NOT NULL DEFAULT 0)',
                'CREATE TABLE permissions (id INTEGER PRIMARY KEY, name TEXT UNIQUE, '
                'description TEXT, created_at DATETIME NOT NULL)',
                'CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT UNIQUE, '
                'description TEXT, created_at DATETIME NOT NULL)',
                'CREATE TABLE role_permission (role_id INTEGER, permission_id INTEGER, '
                'PRIMARY KEY (role_id, permission_id))',
                'CREATE TABLE user_role (user_id INTEGER, role_id INTEGER)',
                'CREATE TABLE ai_model_configs (id INTEGER PRIMARY KEY, model_key TEXT)',
                'CREATE TABLE report_runs (id TEXT PRIMARY KEY, result_json TEXT)',
                'CREATE TABLE ai_usage_events (id INTEGER PRIMARY KEY, total_cost_usd FLOAT)',
                "INSERT INTO ai_model_configs VALUES (1, 'existing-model')",
                "INSERT INTO report_runs VALUES ('existing-run', '{\"schema_version\":\"1.3.1\"}')",
                'INSERT INTO ai_usage_events VALUES (1, 2.5)',
                "INSERT INTO reports VALUES (1, 'existing report')",
                'INSERT INTO public_links VALUES (1, 1, 1, 1)',
            )
            for statement in statements:
                connection.execute(sa.text(statement))
            if preexisting_reset:
                connection.execute(sa.text(
                    'ALTER TABLE public_links ADD COLUMN allow_reset_to_default BOOLEAN NOT NULL DEFAULT 0'
                ))
                connection.execute(sa.text('UPDATE public_links SET allow_reset_to_default = 1'))
            with Operations.context(MigrationContext.configure(connection)):
                for migration in migrations:
                    migration.upgrade()
                flags = connection.execute(sa.text(
                    'SELECT allow_refresh, allow_reset_to_default, allow_refresh_visuals, requires_login FROM reports'
                )).one()
                self.assertEqual(tuple(flags), (1, int(preexisting_reset), 0, 0))
                for migration in reversed(migrations):
                    migration.downgrade()
            columns = {c['name'] for c in sa.inspect(connection).get_columns('public_links')}
            self.assertEqual(columns, {'id', 'report_id_fk', 'is_active', 'allow_refresh'})
            self.assertEqual(connection.execute(sa.text('SELECT allow_refresh FROM public_links')).scalar_one(), 1)
            self.assertEqual(connection.execute(sa.text('SELECT model_key FROM ai_model_configs')).scalar_one(), 'existing-model')
            self.assertEqual(connection.execute(sa.text('SELECT id FROM report_runs')).scalar_one(), 'existing-run')
            self.assertEqual(connection.execute(sa.text('SELECT total_cost_usd FROM ai_usage_events')).scalar_one(), 2.5)
        engine.dispose()
