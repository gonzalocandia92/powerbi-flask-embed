#!/usr/bin/env python3
"""
Back up the application database with pg_dump.

Run it from the project root, next to .env:

    python backup_db.py --label pre-hotfix
    python backup_db.py --dry-run        # show what would run, without touching the database

The connection string comes from --uri, then the SQLALCHEMY_DATABASE_URI environment
variable, then the .env file (the same precedence the application uses).

The database is only read: the pre-flight check opens a read-only session and pg_dump
never writes. The dump is written to ./backups (ignored by git) in pg_dump's custom
format, together with a small <file>.info.txt manifest. Restore it with pg_restore.
It holds production data and password hashes: keep it private.

pg_dump must be at least as new as the server. A local pg_dump is used when it is new
enough; otherwise the script runs pg_dump from a postgres Docker image.
"""
import argparse
import datetime
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = PROJECT_ROOT / 'backups'
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
KEY_TABLES = ('reports', 'public_links', 'users', 'alembic_version')


class BackupError(Exception):
    """A problem the user can act on; reported without a traceback."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str
    password: str | None
    dbname: str
    sslmode: str | None = None

    def label(self):
        return f'{self.user}@{self.host}:{self.port}/{self.dbname}'


@dataclass(frozen=True)
class ServerInfo:
    version: str
    major: int
    size: str
    revisions: tuple


# -- configuration ----------------------------------------------------------

def read_uri(cli_uri):
    """Return (uri, where it came from)."""
    if cli_uri:
        return cli_uri, '--uri'
    from_env = os.environ.get('SQLALCHEMY_DATABASE_URI')
    if from_env:
        return from_env, 'variable de entorno'
    env_file = PROJECT_ROOT / '.env'
    if env_file.is_file():
        from dotenv import dotenv_values
        value = dotenv_values(env_file).get('SQLALCHEMY_DATABASE_URI')
        if value:
            return value, '.env'
    raise BackupError(
        'No encontré SQLALCHEMY_DATABASE_URI. Definila en .env, como variable de entorno o con --uri.'
    )


def parse_target(uri):
    from sqlalchemy.engine import make_url

    try:
        url = make_url(uri)
    except Exception as exc:  # malformed URL
        raise BackupError(f'SQLALCHEMY_DATABASE_URI no es una URL válida ({type(exc).__name__}).') from None
    if not url.drivername.startswith('postgresql'):
        raise BackupError(f'Solo se pueden respaldar bases PostgreSQL (el driver es "{url.drivername}").')
    if not (url.host and url.username and url.database):
        raise BackupError('La URL debe incluir usuario, host y nombre de base.')
    sslmode = url.query.get('sslmode')
    if isinstance(sslmode, tuple):
        sslmode = sslmode[-1]
    return Target(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=url.database,
        sslmode=sslmode,
    )


def build_filename(dbname, now, label=None):
    stamp = now.strftime('%Y-%m-%d_%H%M%S')
    clean_label = re.sub(r'[^A-Za-z0-9._-]+', '-', label or '').strip('-')
    parts = [dbname, stamp] + ([clean_label] if clean_label else [])
    return '_'.join(parts) + '.dump'


def process_env(target):
    """Environment for the child process; the password travels here, never in arguments."""
    env = dict(os.environ)
    if target.password:
        env['PGPASSWORD'] = target.password
    if target.sslmode:
        env['PGSSLMODE'] = target.sslmode
    return env


# -- pre-flight (read-only) ---------------------------------------------------

def preflight(target):
    try:
        import psycopg2
    except ImportError:
        raise BackupError('Falta psycopg2. Activá el entorno virtual del proyecto (venv) y reintentá.') from None

    kwargs = dict(
        host=target.host, port=target.port, user=target.user,
        password=target.password, dbname=target.dbname, connect_timeout=15,
    )
    if target.sslmode:
        kwargs['sslmode'] = target.sslmode
    try:
        conn = psycopg2.connect(**kwargs)
    except psycopg2.OperationalError as exc:
        raise BackupError(f'No pude conectarme a {target.label()}: {_scrub(str(exc).strip(), target)}') from None
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute('show server_version')
        version = cur.fetchone()[0]
        cur.execute('show server_version_num')
        major = int(cur.fetchone()[0]) // 10000
        cur.execute('select pg_size_pretty(pg_database_size(current_database()))')
        size = cur.fetchone()[0]
        try:
            cur.execute('select version_num from alembic_version')
            revisions = tuple(row[0] for row in cur.fetchall())
        except psycopg2.Error:
            revisions = ()
        return ServerInfo(version=version, major=major, size=size, revisions=revisions)
    finally:
        conn.close()


def _scrub(text, target):
    return text.replace(target.password, '***') if target.password else text


# -- choosing where pg_dump runs ---------------------------------------------

def find_local_tool(name):
    found = shutil.which(name)
    if found:
        return found
    if os.name == 'nt':
        root = os.environ.get('ProgramFiles', r'C:\Program Files')
        candidates = glob.glob(os.path.join(root, 'PostgreSQL', '*', 'bin', f'{name}.exe'))
        candidates.sort(key=_version_in_path, reverse=True)
        return candidates[0] if candidates else None
    return None


def _version_in_path(path):
    match = re.search(r'PostgreSQL[\\/](\d+)', path)
    return int(match.group(1)) if match else 0


def tool_major(path):
    try:
        out = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'\)\s*(\d+)', out)
    return int(match.group(1)) if match else None


def local_postgres_images():
    """Majors of the postgres/postgis images already present in Docker: [(major, 'repo:tag')]."""
    try:
        out = subprocess.run(
            ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    images = []
    for line in out.splitlines():
        repo, _, tag = line.strip().rpartition(':')
        if repo in ('postgres', 'postgis/postgis'):
            match = re.match(r'(\d+)', tag)
            if match:
                images.append((int(match.group(1)), line.strip()))
    return images


def pick_docker_image(server_major, local_images=None):
    """Prefer an image already on the machine whose major is the smallest one >= the server's."""
    if server_major is None:
        server_major = 16
    images = local_postgres_images() if local_images is None else local_images
    usable = sorted(
        (major, 0 if image.startswith('postgres:') else 1, image)
        for major, image in images if major >= server_major
    )
    return usable[0][2] if usable else f'postgres:{server_major}-alpine'


class Runner:
    """Knows how to build the pg_dump / pg_restore command lines for one execution strategy."""

    def __init__(self, kind, out_dir, pg_dump=None, pg_restore=None, image=None):
        self.kind, self.out_dir = kind, Path(out_dir)
        self.pg_dump, self.pg_restore, self.image = pg_dump, pg_restore, image

    def describe(self):
        return f'pg_dump local ({self.pg_dump})' if self.kind == 'native' else f'Docker, imagen {self.image}'

    def _docker_base(self, with_password):
        cmd = ['docker', 'run', '--rm']
        if with_password:
            cmd += ['-e', 'PGPASSWORD', '-e', 'PGSSLMODE']
        return cmd + ['-v', f'{self.out_dir}:/backup', self.image]

    def dump_cmd(self, target, filename):
        host = target.host
        outfile = str(self.out_dir / filename)
        if self.kind == 'docker':
            outfile = f'/backup/{filename}'
            if host in LOCAL_HOSTS:
                host = 'host.docker.internal'  # the container cannot reach the host through localhost
        args = [
            '-h', host, '-p', str(target.port), '-U', target.user, '-d', target.dbname,
            '-Fc', '-Z', '6', '-f', outfile,
        ]
        if target.password:
            args.append('--no-password')
        if self.kind == 'native':
            return [self.pg_dump] + args
        return self._docker_base(True) + ['pg_dump'] + args

    def list_cmd(self, filename):
        if self.kind == 'native':
            return [self.pg_restore, '-l', str(self.out_dir / filename)]
        return self._docker_base(False) + ['pg_restore', '-l', f'/backup/{filename}']


def choose_runner(server_major, out_dir, force_docker=False):
    if not force_docker:
        pg_dump = find_local_tool('pg_dump')
        pg_restore = find_local_tool('pg_restore')
        if pg_dump and pg_restore:
            major = tool_major(pg_dump)
            if major is None or server_major is None or major >= server_major:
                return Runner('native', out_dir, pg_dump=pg_dump, pg_restore=pg_restore)
            print(f'El pg_dump local es {major} y el servidor es {server_major}: uso Docker.')
        elif not pg_dump:
            print('No encontré pg_dump en esta máquina: uso Docker.')
    if not shutil.which('docker'):
        raise BackupError(
            'No hay pg_dump utilizable ni Docker. Instalá las herramientas cliente de PostgreSQL '
            f'(versión {server_major or "del servidor"} o superior) o Docker Desktop.'
        )
    return Runner('docker', out_dir, image=pick_docker_image(server_major))


# -- running ------------------------------------------------------------------

def run_dump(runner, target, filename):
    cmd = runner.dump_cmd(target, filename)
    result = subprocess.run(cmd, env=process_env(target), capture_output=True, text=True)
    outfile = runner.out_dir / filename
    if result.returncode != 0:
        outfile.unlink(missing_ok=True)
        message = _scrub((result.stderr or result.stdout).strip(), target)
        hint = ''
        if 'version mismatch' in message.lower():
            hint = '\nEl pg_dump es más viejo que el servidor. Reintentá con --docker.'
        raise BackupError(f'pg_dump falló (código {result.returncode}):\n{message}{hint}')
    if not outfile.is_file() or outfile.stat().st_size == 0:
        raise BackupError('pg_dump terminó sin errores pero el archivo no existe o está vacío.')
    try:
        os.chmod(outfile, 0o600)  # no-op on Windows
    except OSError:
        pass
    return outfile


def verify_dump(runner, filename):
    """Read the dump's table of contents and confirm the key tables are inside."""
    result = subprocess.run(runner.list_cmd(filename), capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f'El backup no se pudo leer con pg_restore -l:\n{result.stderr.strip()}')
    toc = [line for line in result.stdout.splitlines() if line and not line.startswith(';')]
    present = {t: any(f' TABLE DATA public {t} ' in line for line in toc) for t in KEY_TABLES}
    return len(toc), present


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024


def write_manifest(outfile, target, info, runner, entries, digest, now):
    lines = [
        f'Backup de {target.dbname} ({target.host}:{target.port})',
        f'Fecha: {now.isoformat(timespec="seconds")}',
        f'Servidor PostgreSQL: {info.version}',
        f'Migracion alembic: {", ".join(info.revisions) or "(sin alembic_version)"}',
        f'Tamaño de la base: {info.size}',
        f'Ejecutado con: {runner.describe()}',
        f'Archivo: {outfile.name} ({human_size(outfile.stat().st_size)}), {entries} objetos',
        f'SHA-256: {digest}',
        '',
        'Restaurar en una base vacia:',
        f'  pg_restore -d <base_destino> --no-owner {outfile.name}',
    ]
    manifest = outfile.with_name(outfile.name + '.info.txt')
    manifest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return manifest


# -- command line -------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description='Backup de la base de datos con pg_dump (solo lectura).')
    parser.add_argument('--uri', help='URL de conexión (por defecto SQLALCHEMY_DATABASE_URI del entorno o de .env)')
    parser.add_argument('--label', help='texto para el nombre del archivo, ej. pre-hotfix')
    parser.add_argument('--out-dir', default=str(DEFAULT_OUT_DIR), help='carpeta de destino (por defecto ./backups)')
    parser.add_argument('--docker', action='store_true', help='usar pg_dump desde una imagen de Docker')
    parser.add_argument('--dry-run', action='store_true', help='mostrar qué se ejecutaría, sin conectarse')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    uri, source = read_uri(args.uri)
    target = parse_target(uri)
    out_dir = Path(args.out_dir).resolve()
    now = datetime.datetime.now()
    filename = build_filename(target.dbname, now, args.label)

    print(f'Base de datos: {target.label()}  (URL tomada de: {source})')
    print(f'Destino      : {out_dir / filename}')

    if args.dry_run:
        runner = choose_runner(None, out_dir, args.docker)
        print(f'Se ejecutaría con: {runner.describe()}')
        print('Comando (la contraseña viaja en PGPASSWORD, no en la línea de comandos):')
        print('  ' + ' '.join(runner.dump_cmd(target, filename)))
        print('Modo --dry-run: no me conecté a la base ni escribí archivos.')
        return 0

    print('Comprobando la conexión (sesión de solo lectura)...')
    info = preflight(target)
    print(f'Servidor     : PostgreSQL {info.version}, base de {info.size}')
    print(f'Migración    : {", ".join(info.revisions) or "(la base no tiene alembic_version)"}')

    out_dir.mkdir(parents=True, exist_ok=True)
    runner = choose_runner(info.major, out_dir, args.docker)
    print(f'Generando el backup con: {runner.describe()}  (puede tardar)...')
    outfile = run_dump(runner, target, filename)

    entries, present = verify_dump(runner, filename)
    missing = [t for t, ok in present.items() if not ok]
    digest = sha256_of(outfile)
    manifest = write_manifest(outfile, target, info, runner, entries, digest, now)

    print()
    print(f'Backup listo : {outfile}')
    print(f'Tamaño       : {human_size(outfile.stat().st_size)}   objetos: {entries}')
    print(f'SHA-256      : {digest}')
    print(f'Manifiesto   : {manifest}')
    print('Tablas clave : ' + ', '.join(f'{t}={"ok" if ok else "FALTA"}' for t, ok in present.items()))
    if missing:
        print(f'ATENCION: no encontré datos de {", ".join(missing)} en el backup. Revisalo antes de confiar en él.')
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BackupError as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Cancelado.', file=sys.stderr)
        sys.exit(130)
