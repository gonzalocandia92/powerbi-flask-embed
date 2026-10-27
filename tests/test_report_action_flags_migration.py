"""
Tests for the migration that moves allow_refresh / allow_reset_to_default from
public_links to reports.

The full migration chain relies on PostgreSQL features, so this runs only this
revision against minimal SQLite tables.
"""
import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / 'migrations' / 'versions' / '91af0b7c2d11_move_link_actions_to_report.py'
)


def _load_migration():
    spec = importlib.util.spec_from_file_location('move_link_actions_to_report', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MoveLinkActionsToReportMigrationTest(unittest.TestCase):

    def setUp(self):
        self.migration = _load_migration()
        self.engine = sa.create_engine('sqlite://')
        self.conn = self.engine.connect()
        self.conn.execute(sa.text("CREATE TABLE reports (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL)"))
        self.conn.execute(sa.text(
            "CREATE TABLE public_links ("
            " id INTEGER PRIMARY KEY,"
            " report_id_fk INTEGER NOT NULL REFERENCES reports(id),"
            " custom_slug VARCHAR(120),"
            " is_active BOOLEAN,"
            " allow_refresh BOOLEAN NOT NULL DEFAULT 0,"
            " allow_reset_to_default BOOLEAN NOT NULL DEFAULT 0)"
        ))
        for report_id in (1, 2, 3, 4):
            self.conn.execute(sa.text("INSERT INTO reports (id, name) VALUES (:id, 'r')"), {'id': report_id})
        links = [
            # report 1: active links disagree on allow_refresh
            (1, 1, 'r1-a', True, True, False),
            (2, 1, 'r1-b', True, False, False),
            # report 2: the only link with the flag is inactive
            (3, 2, 'r2-a', False, True, True),
            (4, 2, 'r2-b', True, False, False),
            # report 4: reset enabled on its only link
            (5, 4, 'r4-a', True, False, True),
        ]
        for link in links:
            self.conn.execute(
                sa.text(
                    "INSERT INTO public_links "
                    "(id, report_id_fk, custom_slug, is_active, allow_refresh, allow_reset_to_default) "
                    "VALUES (:id, :rid, :slug, :active, :refresh, :reset)"
                ),
                dict(zip(('id', 'rid', 'slug', 'active', 'refresh', 'reset'), link)),
            )
        self.conn.commit()

        self.ctx = MigrationContext.configure(self.conn)
        self.ops = Operations(self.ctx)

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _run(self, fn):
        with Operations.context(self.ctx):
            fn()
        self.conn.commit()

    def _columns(self, table):
        return {c['name'] for c in sa.inspect(self.conn).get_columns(table)}

    def _report_flags(self):
        rows = self.conn.execute(sa.text(
            "SELECT id, allow_refresh, allow_reset_to_default, allow_refresh_visuals "
            "FROM reports ORDER BY id"
        )).fetchall()
        return {r[0]: (bool(r[1]), bool(r[2]), bool(r[3])) for r in rows}

    def test_upgrade_moves_columns(self):
        self._run(self.migration.upgrade)

        self.assertTrue({'allow_refresh', 'allow_reset_to_default', 'allow_refresh_visuals'} <= self._columns('reports'))
        self.assertFalse({'allow_refresh', 'allow_reset_to_default'} & self._columns('public_links'))

    def test_upgrade_enables_report_when_any_active_link_had_it(self):
        self._run(self.migration.upgrade)

        flags = self._report_flags()
        self.assertEqual(flags[1], (True, False, False))   # one active link had allow_refresh
        self.assertEqual(flags[2], (False, False, False))  # flagged link was inactive
        self.assertEqual(flags[3], (False, False, False))  # no links
        self.assertEqual(flags[4], (False, True, False))   # reset enabled on its link

    def test_upgrade_warns_about_reports_whose_links_disagreed(self):
        with self.assertLogs('alembic.runtime.migration', level='WARNING') as logs:
            self._run(self.migration.upgrade)

        joined = '\n'.join(logs.output)
        self.assertIn('allow_refresh', joined)
        self.assertIn('[1]', joined)
        self.assertNotIn('allow_reset_to_default differed', joined)

    def test_upgrade_keeps_link_rows(self):
        self._run(self.migration.upgrade)

        count = self.conn.execute(sa.text("SELECT COUNT(*) FROM public_links")).scalar()
        self.assertEqual(count, 5)

    def test_downgrade_restores_columns_from_report_values(self):
        self._run(self.migration.upgrade)
        self._run(self.migration.downgrade)

        self.assertTrue({'allow_refresh', 'allow_reset_to_default'} <= self._columns('public_links'))
        self.assertFalse(
            {'allow_refresh', 'allow_reset_to_default', 'allow_refresh_visuals'} & self._columns('reports')
        )
        rows = self.conn.execute(sa.text(
            "SELECT id, allow_refresh, allow_reset_to_default FROM public_links ORDER BY id"
        )).fetchall()
        by_link = {r[0]: (bool(r[1]), bool(r[2])) for r in rows}
        self.assertEqual(by_link[1], (True, False))  # report 1 had allow_refresh -> all its links
        self.assertEqual(by_link[2], (True, False))
        self.assertEqual(by_link[3], (False, False))
        self.assertEqual(by_link[5], (False, True))


if __name__ == '__main__':
    unittest.main()
