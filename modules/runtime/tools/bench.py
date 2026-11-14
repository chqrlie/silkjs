#!/usr/bin/env python3
"""The runtime benchmarks against Bun with its JIT off: setup, the runs, and what must hold for both.

`silk task bench-setup`, `bench`, `bench-mem`, `bench-cold`, `bench-all` and `offline-deps` run this.
The measurements are bench/bench.py, bench/mem.py and bench/coldstart/coldstart.py; this program
prepares what they need and refuses to start them in a state that gives wrong numbers:

  * BUN_OPTIONS and NODE_OPTIONS are dropped (a cloud container exports BUN_OPTIONS=--smol, which
    makes Bun about 25% slower; only the bun_nojit_smol column sets it, on purpose);
  * silkjs runs with SILKJS_PREPARSE_THREADS=0 and Bun with BUN_JSC_useJIT=0;
  * servers run on CPU 0, wrk and the clients on CPUs 2,3;
  * runtimes are interleaved over several rounds, and nothing is measured while a build runs.

  python3 tools/bench.py setup [STEP...]
  python3 tools/bench.py run  [--rounds 6] [--runtime EXE] [--runtimes a,b] [--out DIR] [GROUP...]
  python3 tools/bench.py mem  [--rounds 3] ...
  python3 tools/bench.py cold [--rounds 30] ... [CASE...]
  python3 tools/bench.py all  [--rounds 6] [--mem-rounds 3] [--cold-runs 30] ...
  python3 tools/bench.py deps --from DIR

Everything is written under the work directory: --work, else $SILKJS_BENCH_WORK, else build/bench.
"""
import argparse
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / 'engine' / 'scripts'))
import benchenv  # noqa: E402
from benchenv import fail, sh  # noqa: E402

BENCH = ROOT / 'bench'
BUN_FOLDERS = ['express', 'websocket-server', 'postgres', 'sqlite', 'cat']
NORTHWIND = ('https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/'
             '46d5f8a64f396f87cd374d1600dbf521523980e8/Northwind_large.sqlite.zip')
SETUP_STEPS = ['wrk', 'postgres', 'bunrepo', 'install', 'northwind', 'cert', 'bigfile']
# bench.py's columns. `bun` (JIT on) is context for the cold start table only.
RUNTIMES = ['silkjs', 'bun_nojit', 'bun_nojit_smol']
BRANCH_OVERRIDES = {'brotli': ('https://github.com/google/brotli', 'ed738e842d2fbdf2d6459e39267a633c4a9b2f5d'),
                    'libuv': ('https://github.com/libuv/libuv', 'v1.51.0')}


def root_command(argv):
    """A command that needs root: as is for root, through sudo otherwise."""
    if os.geteuid() == 0:
        return argv
    if not shutil.which('sudo'):
        fail(f'{" ".join(argv)} needs root; run it yourself, then rerun this step')
    return ['sudo', *argv]


# ---------------------------------------------------------------- setup

def setup(args):
    work = benchenv.work_directory(args.work, ROOT / 'build/bench')
    environment = benchenv.clean_environment()
    bun_bench = work / 'bunrepo/bench'
    steps = benchenv.Steps(args.names, SETUP_STEPS)

    def wrk():
        if shutil.which('wrk'):
            return shutil.which('wrk')
        if not benchenv.apt_install(['wrk']):
            raise RuntimeError('apt-get could not install wrk; install it (https://github.com/wg/wrk) and rerun')

    def postgres():
        if not shutil.which('psql'):
            benchenv.apt_install(['postgresql'])
        if subprocess.run(['pg_isready', '-q', '-h', 'localhost']).returncode != 0:
            sh(root_command(['service', 'postgresql', 'start']))
        # The benchmark connects as postgres:postgres on localhost (bench/bench.py).
        alter = "ALTER USER postgres PASSWORD 'postgres';"
        if os.geteuid() == 0:
            sh(['su', 'postgres', '-c', f'psql -c "{alter}"'])
        else:
            sh(['sudo', '-u', 'postgres', 'psql', '-c', alter])
        sh(['psql', '-tA', '-c', 'select version()'], env=dict(environment, PGUSER='postgres', PGPASSWORD='postgres',
                                                               PGHOST='localhost', PGDATABASE='postgres'))

    def bunrepo():
        repo = work / 'bunrepo'
        if not (repo / '.git').is_dir():
            sh(['git', 'clone', '-q', '--depth', '1', '--filter=blob:none', '--sparse', 'https://github.com/oven-sh/bun', repo])
        sh(['git', '-C', repo, 'sparse-checkout', 'set', *('bench/' + folder for folder in BUN_FOLDERS)])
        revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        return f'oven-sh/bun {revision[:12]}'

    def install():
        bun = benchenv.executable(args.bun, 'BUN', 'bun', 'bun', 'Install Bun (https://bun.sh) or set BUN.')
        notes = []
        for folder in ['express', 'websocket-server', 'postgres', 'sqlite']:
            print(f'   $ (cd bunrepo/bench/{folder} && bun install)', flush=True)
            done = subprocess.run([bun, 'install'], cwd=bun_bench / folder, env=environment)
            if done.returncode != 0:
                # sqlite: better-sqlite3 has no prebuilt binary for Bun; the benchmark uses bun:sqlite and node:sqlite.
                if folder != 'sqlite':
                    raise RuntimeError(f'bun install failed in bench/{folder}')
                notes.append('sqlite: install error ignored (better-sqlite3 is not used)')
        return '; '.join(notes)

    def northwind():
        database = bun_bench / 'sqlite/src/northwind.sqlite'
        if database.is_file() and database.stat().st_size > 1 << 20:
            return str(database)
        downloads = work / 'downloads'
        downloads.mkdir(exist_ok=True)
        benchenv.download(NORTHWIND, downloads / 'northwind.zip')
        sh(['unzip', '-oq', downloads / 'northwind.zip', '-d', downloads])
        database.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(downloads / 'Northwind_large.sqlite', database)

    def cert():
        express = bun_bench / 'express'
        if (express / 'cert.pem').is_file() and subprocess.run(
                ['openssl', 'x509', '-checkend', '86400', '-noout', '-in', str(express / 'cert.pem')],
                capture_output=True).returncode == 0:
            return 'still valid'
        sh(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'key.pem', '-out', 'cert.pem',
            '-days', '30', '-subj', '/CN=localhost'], cwd=express, stderr=subprocess.DEVNULL)

    def bigfile():
        big = work / '1g.bin'
        if big.is_file() and big.stat().st_size == 1 << 30:
            return str(big)
        sh(['head', '-c', str(1 << 30), '/dev/urandom'], stdout=open(big, 'wb'))

    for name, action in (('wrk', wrk), ('postgres', postgres), ('bunrepo', bunrepo), ('install', install),
                         ('northwind', northwind), ('cert', cert), ('bigfile', bigfile)):
        steps.step(name, action)
    print(f'\nwork directory: {work}')
    steps.finish()


# ---------------------------------------------------------------- the runs

class Run:
    """What the three measurements share: executables, the column set, the environment, the output folder."""

    def __init__(self, args, allowed=RUNTIMES):
        self.work = benchenv.work_directory(args.work, ROOT / 'build/bench')
        names = (args.runtimes or os.environ.get('RUNTIMES') or ','.join(RUNTIMES)).split(',')
        unknown = [name for name in names if name not in allowed]
        if unknown:
            fail(f'unknown runtime(s) {", ".join(unknown)}; this task takes: {", ".join(allowed)}')
        self.names = names
        self.runtime = None
        if 'silkjs' in names:
            self.runtime = benchenv.executable(
                args.runtime, 'SILKJS_RUNTIME', ROOT / 'build/silkjsruntime', 'the runtime executable',
                'Build it first with `silk task build --release`, or pass --runtime / set SILKJS_RUNTIME.')
        self.bun = None
        if any(name.startswith('bun') for name in names):
            self.bun = benchenv.executable(args.bun, 'BUN', 'bun', 'bun', 'Install Bun (https://bun.sh) or set BUN.')
        self.environment = benchenv.clean_environment({
            'SILKJS_BENCH_WORK': str(self.work), 'RUNTIMES': ','.join(names), 'BUN': self.bun or 'bun',
            'RT': self.runtime or str(ROOT / 'build/silkjsruntime')})
        self.environment.pop('SILKJS_RUNTIME', None)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
        self.out = Path(args.out).expanduser().resolve() if args.out else self.work / 'results' / stamp
        self.out.mkdir(parents=True, exist_ok=True)
        self.force = args.force
        self.check_bun()

    def check_bun(self):
        """Bun must start without --smol once BUN_OPTIONS is dropped."""
        if not self.bun:
            return
        seen = subprocess.run([self.bun, '-e', 'console.log(JSON.stringify(process.execArgv))'], env=self.environment,
                              capture_output=True, text=True)
        if seen.returncode != 0 or 'smol' in seen.stdout:
            fail(f'bun still starts with {seen.stdout.strip() or seen.stderr.strip()} after dropping BUN_OPTIONS; '
                 'check bunfig.toml for smol = true')

    def announce(self, title, extra):
        versions = {}
        if self.runtime:
            versions['silkjs'] = self.runtime
        if self.bun:
            version = subprocess.run([self.bun, '--version'], env=self.environment, capture_output=True, text=True)
            versions['bun'] = f'{self.bun} ({version.stdout.strip()})'
        benchenv.announce(title, {**versions, 'runtimes': ','.join(self.names), 'work': self.work, 'out': self.out, **extra})

    def require_setup(self, groups):
        """The files `bench-setup` makes; a missing one names the step that makes it."""
        bun_bench = self.work / 'bunrepo/bench'
        wants = lambda *names: not groups or any(name in groups for name in names)  # noqa: E731
        needs = []
        if wants('http', 'serve', 'node_http', 'express_https'):
            needs += [(shutil.which('wrk'), 'wrk'), ((bun_bench / 'express/node_modules').is_dir(), 'bunrepo install'),
                      ((bun_bench / 'express/cert.pem').is_file(), 'cert')]
        if wants('ws', 'ws_api', 'ws_bun_publish'):
            needs += [((bun_bench / 'websocket-server/node_modules').is_dir(), 'bunrepo install')]
        if wants('pg', 'postgres'):
            needs += [((bun_bench / 'postgres/node_modules').is_dir(), 'bunrepo install'),
                      (shutil.which('pg_isready') and subprocess.run(['pg_isready', '-q', '-h', 'localhost']).returncode == 0, 'postgres')]
        if wants('sqlite'):
            needs += [((bun_bench / 'sqlite/src/northwind.sqlite').is_file(), 'bunrepo northwind')]
        if wants('cat', 'cat_streams', 'cat_bun_write'):
            needs += [((self.work / '1g.bin').is_file(), 'bigfile'), ((bun_bench / 'cat/bun.js').is_file(), 'bunrepo')]
        missing = sorted({step for present, step in needs if not present})
        if missing:
            fail(f'the work directory {self.work} is not set up. Run: silk task bench-setup {" ".join(missing)}')

    def python(self, script, arguments, log):
        return benchenv.run([sys.executable, '-u', BENCH / script, *arguments], cwd=self.work, env=self.environment,
                            log=self.out / log)


def run_bench(run, rounds, groups):
    benchenv.check_rounds(rounds, 3)
    run.require_setup(groups)
    if not groups or any(group in groups for group in ('http', 'ws', 'serve', 'node_http', 'express_https', 'ws_api', 'ws_bun_publish')):
        benchenv.require_pinning()
    benchenv.refuse_while_building(run.force)
    run.announce('runtime benchmarks (bench/bench.py)', {
        'rounds': rounds, 'groups': ','.join(groups) or 'all',
        'pinning': f'servers on CPU {benchenv.SERVER_CPU}, wrk and clients on CPUs {benchenv.CLIENT_CPUS}'})
    arguments = [rounds, run.out / 'res.json'] + (['--only', ','.join(groups)] if groups else [])
    status = run.python('bench.py', arguments, 'res.log')
    if status != 0:
        fail(f'bench.py exited {status}; see {run.out / "res.log"}')
    status = run.python('report.py', [run.out / 'results.csv', run.out / 'res.json'], 'report.txt')
    print(f'medians: {run.out / "results.csv"} (server peak RSS and user us/request are columns), '
          f'samples: {run.out / "res.json"}')
    return status


def run_mem(run, rounds):
    benchenv.check_rounds(rounds, 3)
    run.require_setup(['pg', 'sqlite', 'cat'])
    benchenv.refuse_while_building(run.force)
    run.announce('peak RSS of the non-server benchmarks (bench/mem.py)', {'rounds': rounds})
    status = run.python('mem.py', [rounds, run.out / 'mem.json'], 'mem.log')
    print(f'samples: {run.out / "mem.json"}')
    return status


def run_cold(run, runs, cases):
    benchenv.check_rounds(runs, 10)
    benchenv.refuse_while_building(run.force)
    # coldstart.py splits each command on spaces, so no path may hold one.
    commands = {
        'silkjs': f'/usr/bin/env SILKJS_PREPARSE_THREADS=0 {run.runtime}',
        'bun_nojit': f'/usr/bin/env BUN_JSC_useJIT=0 {run.bun}',
        'bun_nojit_smol': f'/usr/bin/env BUN_JSC_useJIT=0 BUN_OPTIONS=--smol {run.bun}',
        'bun': f'{run.bun}',
    }
    columns = [f'{name}={commands[name]}' for name in run.names]
    for path in (run.runtime, run.bun):
        if path and ' ' in path:
            fail(f'coldstart.py splits commands on spaces, and {path!r} has one; use a path without spaces')
    run.announce('cold start (bench/coldstart/coldstart.py)', {'runs': runs, 'cases': ','.join(cases) or 'all'})
    arguments = [runs, run.out / 'cold.json', *columns] + (['--only', ','.join(cases)] if cases else [])
    status = run.python('coldstart/coldstart.py', arguments, 'cold.txt')
    print(f'table: {run.out / "cold.txt"}, medians: {run.out / "cold.json"}')
    return status


COLD_RUNTIMES = RUNTIMES + ['bun']


def bench(args):
    sys.exit(run_bench(Run(args), args.rounds or 6, args.names))


def mem(args):
    sys.exit(run_mem(Run(args), args.rounds or 3))


def cold(args):
    sys.exit(run_cold(Run(args, COLD_RUNTIMES), args.rounds or 30, args.names))


def everything(args):
    """The three measurements into one folder, as the handoff's full2.sh did."""
    run = Run(args, COLD_RUNTIMES)
    table = Run(args, COLD_RUNTIMES)  # bench.py and mem.py have no JIT column: `bun` is for the cold start table
    table.out = run.out
    table.names = [name for name in run.names if name != 'bun']
    table.environment['RUNTIMES'] = ','.join(table.names)
    statuses = [run_bench(table, args.rounds or 6, []), run_mem(table, args.mem_rounds or 3),
                run_cold(run, args.cold_runs or 30, [])]
    print(f'all results: {run.out}')
    sys.exit(max(statuses))


# ---------------------------------------------------------------- offline dependencies

def deps(args):
    """Unpacks prepared copies of what the build downloads, for a network that blocks
    codeload.github.com, sqlite.org and dist.libuv.org, and points Silk at them."""
    source = args.source or os.environ.get('SILKJS_OFFLINE_DEPS')
    if not source:
        fail('say where the prepared archives are: --from DIR or SILKJS_OFFLINE_DEPS=DIR. It must hold '
             'runtime-native-prefix.tgz, runtime-sqlite-amalgamation.tgz, runtime-zstd-nghttp2-system.tgz '
             'and runtime-build-deps.tgz. On an open network this task is not needed: `silk task build` downloads everything.')
    source = Path(source).expanduser().resolve()

    def archive(name):
        found = sorted(source.glob(f'*{name}'))
        if not found:
            fail(f'{name} is missing from {source}')
        return found[-1]

    archives = {name: archive(name) for name in ('runtime-native-prefix.tgz', 'runtime-sqlite-amalgamation.tgz',
                                                 'runtime-zstd-nghttp2-system.tgz', 'runtime-build-deps.tgz')}
    work = benchenv.work_directory(args.work, ROOT / 'build/bench')
    local = work / 'deps'
    build_deps = ROOT / 'build/deps'
    for directory in (local, local / 'src', build_deps):
        directory.mkdir(parents=True, exist_ok=True)
    sh(['tar', 'xzf', archives['runtime-native-prefix.tgz'], '-C', local])        # prefix/: OpenSSL, libuv
    sh(['tar', 'xzf', archives['runtime-sqlite-amalgamation.tgz'], '-C', local])  # bsq/package/deps/sqlite3
    sh(['tar', 'xzf', archives['runtime-build-deps.tgz'], '-C', build_deps])
    # Static zstd, nghttp2 and Brotli where the linker looks. This one writes under /usr.
    sh(root_command(['tar', 'xzf', str(archives['runtime-zstd-nghttp2-system.tgz']), '-C', '/']))
    multiarch = Path('/usr/lib') / (os.uname().machine + '-linux-gnu')
    for pattern in ('libzstd*.a', 'libnghttp2*.a', 'libbrotli*.a'):
        for library in sorted(multiarch.glob(pattern)):
            sh(root_command(['ln', '-sf', str(library), '/usr/lib/']))
    for name, (url, revision) in BRANCH_OVERRIDES.items():
        checkout = local / 'src' / name
        if not (checkout / '.git').is_dir():
            sh(['git', 'init', '-q', checkout])
        if subprocess.run(['git', '-C', str(checkout), 'rev-parse', '-q', '--verify', 'HEAD'], capture_output=True).returncode != 0:
            sh(['git', '-C', checkout, 'fetch', '-q', '--depth', '1', url, revision])
            sh(['git', '-C', checkout, 'checkout', '-q', 'FETCH_HEAD'])
    # The build's own archive cache holds simde as a tarball; an override wants the unpacked source.
    packed = sorted(build_deps.glob('simde-*.tar.gz'))
    if not packed:
        fail(f'runtime-build-deps.tgz held no simde-*.tar.gz (looked in {build_deps})')
    simde = packed[-1].with_name(packed[-1].name[:-len('.tar.gz')])
    if not simde.is_dir():
        simde.mkdir()
        sh(['tar', 'xzf', packed[-1], '-C', simde, '--strip-components', '1'])
    overrides = (f'[native.override.sqlite]\npath = "{local}/bsq/package/deps/sqlite3"\n'
                 f'[native.override.silkjs-simde]\npath = "{simde}"\n'
                 f'[native.override.silkjs-brotli]\npath = "{local}/src/brotli"\n'
                 f'[native.override.silkjs-uv]\npath = "{local}/src/libuv"\n')
    home = Path(os.environ.get('SILK_HOME') or '~/.silk').expanduser()
    home.mkdir(parents=True, exist_ok=True)
    target = home / 'native-overrides.toml'
    if target.is_file() and target.read_text() != overrides:
        kept = target.with_name('native-overrides.toml.before-offline-deps')
        target.replace(kept)
        print(f'   kept the previous {target} as {kept.name}')
    target.write_text(overrides)
    print(f'wrote {target}:\n{overrides}')
    print('If a pinned revision or URL changes, compare [native.archive.*] in silk.toml and ../native/*/silk.toml.\n'
          'Now build, and check that the log says "Embedded ... bytes of built-in bytecode" and "Embedded a heap image":\n'
          f'  PKG_CONFIG_PATH={local}/prefix/lib/pkgconfig silk task build --release --output {work}/rt')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    def command(name, function, names_help=None, measures=True):
        sub = commands.add_parser(name)
        sub.set_defaults(function=function)
        sub.add_argument('--work', help='work directory (default $SILKJS_BENCH_WORK, else build/bench)')
        sub.add_argument('--bun', help='the Bun executable (default $BUN, else bun on PATH)')
        if names_help:
            sub.add_argument('names', nargs='*', help=names_help)
        if measures:
            sub.add_argument('--rounds', type=int, help='interleaved rounds')
            sub.add_argument('--runtime', help='the runtime executable (default $SILKJS_RUNTIME, else build/silkjsruntime)')
            sub.add_argument('--runtimes', help=f'comma-separated columns (default $RUNTIMES, else {",".join(RUNTIMES)})')
            sub.add_argument('--out', help='output directory (default <work>/results/<UTC time>)')
            sub.add_argument('--force', action='store_true', help='measure even while a build runs or the host is busy')
        return sub

    command('setup', setup, 'steps to run (default all): ' + ' '.join(SETUP_STEPS), measures=False)
    command('run', bench, 'groups or benchmarks: http ws pg sqlite spawn cat imm, or a name such as node_http')
    command('mem', mem)
    command('cold', cold, 'cases: empty hello cjs esm local fsread net timer')
    both = command('all', everything)
    both.add_argument('--mem-rounds', type=int)
    both.add_argument('--cold-runs', type=int)
    offline = command('deps', deps, measures=False)
    offline.add_argument('--from', dest='source', help='folder with the prepared archives (default $SILKJS_OFFLINE_DEPS)')
    args = parser.parse_args()
    args.function(args)


if __name__ == '__main__':
    main()
