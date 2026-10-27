"""
Tests for backup_db.py (pg_dump wrapper). No database or Docker is needed: every
external call is mocked.
"""
import datetime
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import backup_db as b

PASSWORD = 'S3cr3t-Pass'
URI = f'postgresql+psycopg2://appuser:{PASSWORD}@db.example.invalid:5433/flask_embed?sslmode=require'


def _target(host='db.example.invalid', password=PASSWORD):
    return b.Target(host=host, port=5432, user='appuser', password=password, dbname='flask_embed')


class ParseTargetTest(unittest.TestCase):

    def test_reads_every_part_of_the_url(self):
        target = b.parse_target(URI)

        self.assertEqual(
            (target.host, target.port, target.user, target.password, target.dbname, target.sslmode),
            ('db.example.invalid', 5433, 'appuser', PASSWORD, 'flask_embed', 'require'),
        )

    def test_url_encoded_password_is_decoded(self):
        target = b.parse_target('postgresql://u:p%40ss%2Fword@h/db')

        self.assertEqual(target.password, 'p@ss/word')

    def test_default_port(self):
        self.assertEqual(b.parse_target('postgresql://u:p@h/db').port, 5432)

    def test_rejects_other_databases(self):
        with self.assertRaises(b.BackupError):
            b.parse_target('sqlite:///local.db')

    def test_rejects_urls_without_user_or_database(self):
        for uri in ('postgresql://h/db', 'postgresql://u:p@h/'):
            with self.subTest(uri), self.assertRaises(b.BackupError):
                b.parse_target(uri)

    def test_label_does_not_include_the_password(self):
        self.assertNotIn(PASSWORD, b.parse_target(URI).label())


class FilenameTest(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 6, 19, 5, 9)

    def test_with_label(self):
        self.assertEqual(
            b.build_filename('flask_embed', self.NOW, 'pre hotfix!'),
            'flask_embed_2026-10-06_190509_pre-hotfix.dump',
        )

    def test_without_label(self):
        self.assertEqual(b.build_filename('flask_embed', self.NOW), 'flask_embed_2026-10-06_190509.dump')

    def test_label_cannot_escape_the_folder(self):
        name = b.build_filename('db', self.NOW, '../../etc/passwd')

        self.assertNotIn('/', name)
        self.assertNotIn('\\', name)


class ReadUriTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / '.env').write_text('SQLALCHEMY_DATABASE_URI=postgresql://u:p@from-env-file/db\n')
        root = patch.object(b, 'PROJECT_ROOT', Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)

    def _environ(self, **extra):
        clean = {k: v for k, v in os.environ.items() if k != 'SQLALCHEMY_DATABASE_URI'}
        clean.update(extra)
        return patch.dict(os.environ, clean, clear=True)

    def test_env_file_is_the_last_resort(self):
        with self._environ():
            uri, source = b.read_uri(None)

        self.assertEqual(source, '.env')
        self.assertIn('from-env-file', uri)

    def test_environment_variable_beats_the_env_file(self):
        with self._environ(SQLALCHEMY_DATABASE_URI='postgresql://u:p@from-environ/db'):
            uri, source = b.read_uri(None)

        self.assertEqual(source, 'variable de entorno')
        self.assertIn('from-environ', uri)

    def test_cli_argument_beats_everything(self):
        with self._environ(SQLALCHEMY_DATABASE_URI='postgresql://u:p@from-environ/db'):
            uri, source = b.read_uri('postgresql://u:p@from-cli/db')

        self.assertEqual((source, uri), ('--uri', 'postgresql://u:p@from-cli/db'))

    def test_missing_everywhere(self):
        (Path(self.tmp.name) / '.env').unlink()
        with self._environ(), self.assertRaises(b.BackupError):
            b.read_uri(None)


class RunnerTest(unittest.TestCase):

    def setUp(self):
        self.out_dir = Path('backups')

    def test_native_command_keeps_the_password_out_of_the_arguments(self):
        runner = b.Runner('native', self.out_dir, pg_dump='pg_dump', pg_restore='pg_restore')

        cmd = runner.dump_cmd(_target(), 'x.dump')

        self.assertNotIn(PASSWORD, ' '.join(cmd))
        self.assertIn('--no-password', cmd)
        self.assertEqual(cmd[0], 'pg_dump')
        self.assertIn('-Fc', cmd)

    def test_docker_command_forwards_the_password_by_name_only(self):
        runner = b.Runner('docker', self.out_dir, image='postgres:16-alpine')

        cmd = runner.dump_cmd(_target(), 'x.dump')

        self.assertNotIn(PASSWORD, ' '.join(cmd))
        self.assertIn('PGPASSWORD', cmd)  # "-e PGPASSWORD" takes the value from the environment
        self.assertEqual(cmd[cmd.index('PGPASSWORD') - 1], '-e')
        self.assertIn('/backup/x.dump', cmd)

    def test_docker_reaches_a_local_database_through_the_host_alias(self):
        runner = b.Runner('docker', self.out_dir, image='postgres:16-alpine')

        local = runner.dump_cmd(_target(host='127.0.0.1'), 'x.dump')
        remote = runner.dump_cmd(_target(host='db.example.invalid'), 'x.dump')

        self.assertIn('host.docker.internal', local)
        self.assertNotIn('host.docker.internal', remote)

    def test_the_password_only_travels_in_the_process_environment(self):
        env = b.process_env(_target())

        self.assertEqual(env['PGPASSWORD'], PASSWORD)

    def test_no_password_in_the_url_means_no_pgpassword(self):
        with patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != 'PGPASSWORD'}, clear=True):
            self.assertNotIn('PGPASSWORD', b.process_env(_target(password=None)))

    def test_list_commands_read_the_same_file(self):
        native = b.Runner('native', self.out_dir, pg_dump='pg_dump', pg_restore='pg_restore')
        docker = b.Runner('docker', self.out_dir, image='postgres:16-alpine')

        self.assertEqual(native.list_cmd('x.dump')[:2], ['pg_restore', '-l'])
        self.assertIn('/backup/x.dump', docker.list_cmd('x.dump'))


class ChooseToolTest(unittest.TestCase):

    def test_image_selection_prefers_the_closest_major_that_is_new_enough(self):
        local = [(16, 'postgres:16-alpine'), (17, 'postgis/postgis:17-3.5')]

        self.assertEqual(b.pick_docker_image(16, local), 'postgres:16-alpine')
        self.assertEqual(b.pick_docker_image(15, local), 'postgres:16-alpine')
        self.assertEqual(b.pick_docker_image(17, local), 'postgis/postgis:17-3.5')

    def test_image_selection_prefers_postgres_over_postgis_for_the_same_major(self):
        local = [(17, 'postgis/postgis:17-3.5'), (17, 'postgres:17-alpine')]

        self.assertEqual(b.pick_docker_image(17, local), 'postgres:17-alpine')

    def test_image_selection_falls_back_to_pulling_the_matching_major(self):
        self.assertEqual(b.pick_docker_image(18, [(16, 'postgres:16-alpine')]), 'postgres:18-alpine')
        self.assertEqual(b.pick_docker_image(17, []), 'postgres:17-alpine')

    def test_tool_major_is_parsed_from_the_version_output(self):
        with patch.object(b.subprocess, 'run', return_value=CompletedProcess([], 0, 'pg_dump (PostgreSQL) 17.2\n', '')):
            self.assertEqual(b.tool_major('pg_dump'), 17)
        with patch.object(b.subprocess, 'run', return_value=CompletedProcess([], 0, 'unexpected', '')):
            self.assertIsNone(b.tool_major('pg_dump'))

    def test_new_enough_local_pg_dump_is_used(self):
        with patch.object(b, 'find_local_tool', side_effect=lambda name: name), \
                patch.object(b, 'tool_major', return_value=17):
            runner = b.choose_runner(16, Path('backups'))

        self.assertEqual(runner.kind, 'native')

    def test_old_local_pg_dump_falls_back_to_docker(self):
        with patch.object(b, 'find_local_tool', side_effect=lambda name: name), \
                patch.object(b, 'tool_major', return_value=15), \
                patch.object(b.shutil, 'which', return_value='docker'), \
                patch.object(b, 'local_postgres_images', return_value=[(16, 'postgres:16-alpine')]), \
                redirect_stdout(io.StringIO()):
            runner = b.choose_runner(16, Path('backups'))

        self.assertEqual((runner.kind, runner.image), ('docker', 'postgres:16-alpine'))

    def test_missing_pg_dump_uses_docker(self):
        with patch.object(b, 'find_local_tool', return_value=None), \
                patch.object(b.shutil, 'which', return_value='docker'), \
                patch.object(b, 'local_postgres_images', return_value=[]), \
                redirect_stdout(io.StringIO()):
            runner = b.choose_runner(16, Path('backups'))

        self.assertEqual(runner.kind, 'docker')

    def test_force_docker_ignores_a_local_pg_dump(self):
        with patch.object(b, 'find_local_tool', side_effect=lambda name: name), \
                patch.object(b.shutil, 'which', return_value='docker'), \
                patch.object(b, 'local_postgres_images', return_value=[]):
            runner = b.choose_runner(16, Path('backups'), force_docker=True)

        self.assertEqual(runner.kind, 'docker')

    def test_nothing_available_is_a_clear_error(self):
        with patch.object(b, 'find_local_tool', return_value=None), \
                patch.object(b.shutil, 'which', return_value=None), \
                redirect_stdout(io.StringIO()), self.assertRaises(b.BackupError):
            b.choose_runner(16, Path('backups'))


class RunDumpTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = b.Runner('native', self.tmp.name, pg_dump='pg_dump', pg_restore='pg_restore')

    def test_failure_removes_the_partial_file_and_hides_the_password(self):
        partial = Path(self.tmp.name) / 'x.dump'
        partial.write_bytes(b'partial')
        failure = CompletedProcess([], 1, '', f'connection to server failed for password {PASSWORD}')

        with patch.object(b.subprocess, 'run', return_value=failure):
            with self.assertRaises(b.BackupError) as raised:
                b.run_dump(self.runner, _target(), 'x.dump')

        self.assertNotIn(PASSWORD, str(raised.exception))
        self.assertFalse(partial.exists())

    def test_version_mismatch_suggests_docker(self):
        failure = CompletedProcess([], 1, '', 'pg_dump: error: aborting because of server version mismatch')

        with patch.object(b.subprocess, 'run', return_value=failure):
            with self.assertRaises(b.BackupError) as raised:
                b.run_dump(self.runner, _target(), 'x.dump')

        self.assertIn('--docker', str(raised.exception))

    def test_an_empty_file_is_not_accepted(self):
        def fake_run(cmd, **kwargs):
            (Path(self.tmp.name) / 'x.dump').write_bytes(b'')
            return CompletedProcess(cmd, 0, '', '')

        with patch.object(b.subprocess, 'run', side_effect=fake_run):
            with self.assertRaises(b.BackupError):
                b.run_dump(self.runner, _target(), 'x.dump')

    def test_verify_reports_which_key_tables_are_in_the_dump(self):
        toc = (
            ';\n; Archive created at ...\n'
            '210; 0 16400 TABLE DATA public reports postgres\n'
            '211; 0 16410 TABLE DATA public public_links postgres\n'
            '212; 0 16420 TABLE DATA public alembic_version postgres\n'
        )
        with patch.object(b.subprocess, 'run', return_value=CompletedProcess([], 0, toc, '')):
            entries, present = b.verify_dump(self.runner, 'x.dump')

        self.assertEqual(entries, 3)
        self.assertEqual(present, {'reports': True, 'public_links': True, 'users': False, 'alembic_version': True})

    def test_verify_fails_when_the_dump_cannot_be_read(self):
        with patch.object(b.subprocess, 'run', return_value=CompletedProcess([], 1, '', 'not a valid archive')):
            with self.assertRaises(b.BackupError):
                b.verify_dump(self.runner, 'x.dump')


class DryRunTest(unittest.TestCase):

    def test_dry_run_prints_the_plan_without_connecting_or_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with patch.object(b, 'preflight') as preflight, \
                    patch.object(b, 'choose_runner', return_value=b.Runner('docker', tmp, image='postgres:16-alpine')), \
                    redirect_stdout(out):
                status = b.main(['--uri', URI, '--out-dir', tmp, '--label', 'pre-hotfix', '--dry-run'])

            self.assertEqual(status, 0)
            preflight.assert_not_called()
            self.assertEqual(os.listdir(tmp), [])
        printed = out.getvalue()
        self.assertNotIn(PASSWORD, printed)
        self.assertIn('db.example.invalid', printed)
        self.assertIn('pre-hotfix', printed)


class HumanSizeTest(unittest.TestCase):

    def test_sizes(self):
        self.assertEqual(b.human_size(512), '512 B')
        self.assertEqual(b.human_size(2048), '2.0 KB')
        self.assertEqual(b.human_size(5 * 1024 * 1024), '5.0 MB')


if __name__ == '__main__':
    unittest.main()
