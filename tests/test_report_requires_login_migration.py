"""
Tests for the migration that adds reports.requires_login and seeds the
reports.read permission and the "Lector de reportes" role.

Runs only this revision against minimal SQLite tables, because the full migration
chain relies on PostgreSQL features.
"""
import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / 'migrations' / 'versions' / '91af0b7c2d12_add_report_requires_login.py'
)


def _load_migration():
    spec = importlib.util.spec_from_file_location('add_report_requires_login', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AddReportRequiresLoginMigrationTest(unittest.TestCase):

    def setUp(self):
        self.migration = _load_migration()
        self.engine = sa.create_engine('sqlite://')
        self.conn = self.engine.connect()
        for statement in (
            "CREATE TABLE reports (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL)",
            "CREATE TABLE permissions (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE,"
            " description VARCHAR(500), created_at DATETIME NOT NULL)",
            "CREATE TABLE roles (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE,"
            " description VARCHAR(500), created_at DATETIME NOT NULL)",
            "CREATE TABLE role_permission (role_id INTEGER NOT NULL, permission_id INTEGER NOT NULL,"
            " PRIMARY KEY (role_id, permission_id))",
            "CREATE TABLE user_role (user_id INTEGER NOT NULL, role_id INTEGER NOT NULL,"
            " PRIMARY KEY (user_id, role_id))",
            "INSERT INTO reports (id, name) VALUES (1, 'existing report')",
        ):
            self.conn.execute(sa.text(statement))
        self.conn.commit()
        self.ctx = MigrationContext.configure(self.conn)

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _run(self, fn):
        with Operations.context(self.ctx):
            fn()
        self.conn.commit()

    def _scalar(self, sql, **params):
        return self.conn.execute(sa.text(sql), params).scalar()

    def _report_columns(self):
        return {c['name'] for c in sa.inspect(self.conn).get_columns('reports')}

    def test_upgrade_adds_column_defaulting_to_false(self):
        self._run(self.migration.upgrade)

        self.assertIn('requires_login', self._report_columns())
        self.assertFalse(self._scalar("SELECT requires_login FROM reports WHERE id = 1"))

    def test_upgrade_seeds_permission_role_and_link(self):
        self._run(self.migration.upgrade)

        self.assertEqual(self._scalar("SELECT COUNT(*) FROM permissions WHERE name = 'reports.read'"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM roles WHERE name = 'Lector de reportes'"), 1)
        linked = self._scalar(
            "SELECT COUNT(*) FROM role_permission rp"
            " JOIN roles r ON r.id = rp.role_id JOIN permissions p ON p.id = rp.permission_id"
            " WHERE r.name = 'Lector de reportes' AND p.name = 'reports.read'"
        )
        self.assertEqual(linked, 1)

    def test_upgrade_reuses_existing_permission_and_role(self):
        self.conn.execute(sa.text(
            "INSERT INTO permissions (id, name, created_at) VALUES (7, 'reports.read', '2026-01-01')"
        ))
        self.conn.execute(sa.text(
            "INSERT INTO roles (id, name, created_at) VALUES (9, 'Lector de reportes', '2026-01-01')"
        ))
        self.conn.commit()

        self._run(self.migration.upgrade)

        self.assertEqual(self._scalar("SELECT COUNT(*) FROM permissions WHERE name = 'reports.read'"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM roles WHERE name = 'Lector de reportes'"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM role_permission WHERE role_id = 9 AND permission_id = 7"), 1)

    def test_downgrade_removes_column_seed_and_assignments(self):
        self._run(self.migration.upgrade)
        role_id = self._scalar("SELECT id FROM roles WHERE name = 'Lector de reportes'")
        self.conn.execute(sa.text("INSERT INTO user_role (user_id, role_id) VALUES (5, :r)"), {'r': role_id})
        self.conn.commit()

        self._run(self.migration.downgrade)

        self.assertNotIn('requires_login', self._report_columns())
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM permissions WHERE name = 'reports.read'"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM roles WHERE name = 'Lector de reportes'"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM role_permission"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM user_role"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM reports"), 1)


if __name__ == '__main__':
    unittest.main()
