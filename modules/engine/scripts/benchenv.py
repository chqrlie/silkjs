"""What every benchmark task does before it measures: the environment, the idle check, the CPUs.

Shared by scripts/bench_engines.py here and by the runtime's tools/bench.py.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

# Variables a container may export that change what is measured:
#   BUN_OPTIONS   the cloud container exports --smol, which makes Bun about 25% slower and smaller;
#   NODE_OPTIONS  the runtime rejects --use-openssl-ca, and Node reads it too.
DROPPED = ('BUN_OPTIONS', 'NODE_OPTIONS')
# Processes that mean a build is running. A build takes every CPU, so nothing measured beside it counts.
BUILDERS = {'silkc', 'clang', 'clang++', 'cc1', 'cc1plus', 'ld.lld', 'lld', 'llvm', 'wasm-ld', 'make', 'ninja', 'cargo', 'rustc'}
SERVER_CPU = '0'
CLIENT_CPUS = '2,3'


def fail(message: str):
    raise SystemExit('bench: ' + message)


def clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ without the variables in DROPPED, and silkjs on one thread."""
    environment = {key: value for key, value in os.environ.items() if key not in DROPPED}
    # The comparison is single threaded: no background parse threads in silkjs.
    environment['SILKJS_PREPARSE_THREADS'] = '0'
    environment.update(extra or {})
    return environment


def dropped_now() -> list[str]:
    return [f'{key}={os.environ[key]}' for key in DROPPED if os.environ.get(key)]


def work_directory(given: str | None, default: Path) -> Path:
    """--work, else $SILKJS_BENCH_WORK, else the module's build/bench."""
    chosen = Path(given or os.environ.get('SILKJS_BENCH_WORK') or default).expanduser().resolve()
    chosen.mkdir(parents=True, exist_ok=True)
    return chosen


def executable(given: str | None, variable: str, default, what: str, hint: str) -> str:
    """An executable from an option, else an environment variable, else `default` (a path or a command name)."""
    chosen = given or os.environ.get(variable) or str(default)
    found = shutil.which(chosen) or (str(Path(chosen).expanduser().resolve()) if os.sep in chosen else None)
    if not found or not os.access(found, os.X_OK):
        fail(f'{what} not found at {chosen!r}. {hint}')
    return found


def running_builds() -> list[str]:
    """Command lines of processes that look like a build in progress (Linux /proc only)."""
    found = []
    own = {os.getpid(), os.getppid()}
    for entry in Path('/proc').glob('[0-9]*'):
        if int(entry.name) in own:
            continue
        try:
            words = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if not words or not words[0]:
            continue
        name = os.path.basename(words[0].decode(errors='replace'))
        line = ' '.join(word.decode(errors='replace') for word in words if word)
        building = name in BUILDERS or name.startswith('clang-')
        building = building or (name == 'silk' and len(words) > 1 and any(
            word in (b'build', b'test', b'check') for word in words[1:4]))
        building = building or 'tools/build.py' in line or 'pgo_build.py' in line
        if building:
            found.append(f'{entry.name}: {line[:120]}')
    return found


def refuse_while_building(force: bool):
    """Never build while benchmarking: stop when a compiler is running or the host is busy."""
    builds = running_builds()
    load = os.getloadavg()[0]
    cpus = os.cpu_count() or 1
    problems = []
    if builds:
        problems.append('a build is running:\n  ' + '\n  '.join(builds[:8]))
    if load > 0.5 * cpus:
        problems.append(f'the 1-minute load average is {load:.2f} on {cpus} CPUs: the host is not idle')
    if not problems:
        return
    text = '; '.join(problems)
    if force:
        print(f'bench: WARNING, measuring anyway (--force): {text}', file=sys.stderr)
        return
    fail(text + '\nWait for it to finish, or pass --force to measure anyway (the numbers will not be comparable).')


def require_pinning():
    """The server benchmarks pin the server to CPU 0 and wrk and the clients to CPUs 2,3."""
    if not shutil.which('taskset'):
        fail('taskset is missing (util-linux): the server benchmarks pin servers and clients to separate CPUs')
    allowed = os.sched_getaffinity(0)
    needed = {int(SERVER_CPU)} | {int(cpu) for cpu in CLIENT_CPUS.split(',')}
    if not needed <= allowed:
        fail(f'CPUs {sorted(needed)} are needed (server on {SERVER_CPU}, wrk and clients on {CLIENT_CPUS}); '
             f'this process may run on {sorted(allowed)}. Use a host with at least 4 CPUs.')


def check_rounds(rounds: int, least: int):
    if rounds < 1:
        fail('rounds must be at least 1')
    if rounds < least:
        print(f'bench: WARNING, {rounds} round(s): host wall time swings about 8%, so take at least {least} '
              'before comparing numbers', file=sys.stderr)


def announce(title: str, pairs: dict[str, object]):
    print(f'== {title}')
    for key, value in pairs.items():
        print(f'   {key}: {value}')
    dropped = dropped_now()
    if dropped:
        print(f'   dropped from the environment: {", ".join(dropped)}')
    sys.stdout.flush()


def run(argv, *, cwd=None, env=None, log: Path | None = None) -> int:
    """Runs a command; with `log`, its output goes to the terminal and to that file."""
    argv = [str(word) for word in argv]
    if log is None:
        return subprocess.run(argv, cwd=cwd, env=env).returncode
    with open(log, 'w') as handle:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            handle.write(line)
        return process.wait()


class Steps:
    """Setup steps that each report ok, skipped or failed, so one blocked download does not hide the rest."""

    def __init__(self, only: list[str], known: list[str]):
        unknown = [name for name in only if name not in known]
        if unknown:
            fail(f'unknown setup step(s) {", ".join(unknown)}; the steps are: {", ".join(known)}')
        self.only = only
        self.results: list[tuple[str, str, str]] = []

    def step(self, name: str, action):
        if self.only and name not in self.only:
            return
        print(f'-- {name}', flush=True)
        try:
            note = action() or ''
            self.results.append((name, 'ok', note))
        except (subprocess.CalledProcessError, OSError, SystemExit, RuntimeError) as error:
            self.results.append((name, 'FAILED', str(error)))
            print(f'   FAILED: {error}', file=sys.stderr, flush=True)

    def finish(self):
        print('\nsetup summary')
        for name, state, note in self.results:
            print(f'  {name:12} {state:7} {note}')
        if any(state == 'FAILED' for _, state, _ in self.results):
            raise SystemExit(1)


def sh(argv, **options):
    """A setup command: echoed, and an error when it fails."""
    argv = [str(word) for word in argv]
    print('   $ ' + ' '.join(argv), flush=True)
    subprocess.run(argv, check=True, **options)


def download(url: str, destination: Path):
    if destination.is_file() and destination.stat().st_size > 0:
        return
    partial = destination.with_name(destination.name + '.part')
    sh(['curl', '-fsSL', '--retry', '4', '-o', partial, url])
    partial.rename(destination)


def apt_install(packages: list[str]) -> bool:
    """Installs the first of `packages` apt has. The package lists of a fresh container are often
    empty, so a failed first try refreshes them once."""
    prefix = [] if os.geteuid() == 0 else ['sudo']
    if not shutil.which('apt-get') or (prefix and not shutil.which('sudo')):
        fail(f'cannot install {" or ".join(packages)}: needs apt-get as root; install it yourself and rerun')
    for refreshed in (False, True):
        if refreshed:
            print('   $ apt-get update', flush=True)
            subprocess.run(prefix + ['apt-get', 'update', '-q'], stdout=subprocess.DEVNULL)
        for package in packages:
            command = prefix + ['apt-get', 'install', '-y', '--no-install-recommends', package]
            print('   $ ' + ' '.join(command), flush=True)
            if subprocess.run(command, stdout=subprocess.DEVNULL).returncode == 0:
                return True
    return False
