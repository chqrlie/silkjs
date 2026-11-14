#!/usr/bin/env python3
"""The engine comparison: silkjs against V8 jitless, JSC without its JIT, QuickJS and the wasm builds.

`silk task bench-setup`, `bench-octane`, `bench-micro`, `bench-fixed` and `bench-report` run this.
The measurements are the scripts in bench/engines/ (octane.py, micro.py, fx.py, rep.py, rep-wasm.py);
this program installs the reference engines, writes the engine commands, and keeps what is easy to forget:

  * silkjs runs with SILKJS_PREPARSE_THREADS=0; NODE_OPTIONS and BUN_OPTIONS are dropped;
  * engines are interleaved over several rounds, and nothing is measured while a build runs;
  * Octane runs V8 through bench/engines/noderun.js (code-load needs a global eval), but the micros
    run node directly: through the loader microbench.js reads its own path as a test name;
  * jsc has no `performance`, so its micros run a copy of microbench.js with a preciseTime clock
    in front; without it JSC looks about 4% better than it is;
  * wasmtime gets -W max-wasm-stack=8388608, an absolute script path and --dir /.

  python3 scripts/bench_engines.py setup [STEP...]
  python3 scripts/bench_engines.py octane [--rounds 3] [--engines a,b] [--jit] [BENCHMARK...]
  python3 scripts/bench_engines.py micro  [--rounds 3] [--engines a,b] [TEST...]
  python3 scripts/bench_engines.py fixed  [--rounds 7] [--iterations N] --only richards,deltablue OLD NEW...
  python3 scripts/bench_engines.py report [--native] [--jit-results FILE]

Everything is written under the work directory: --work, else $SILKJS_BENCH_WORK, else build/bench.
"""
import argparse
import glob
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import benchenv
from benchenv import fail, sh
from toolchain import release_binary

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'bench/engines'
MICRO = ROOT / 'bench/microbench.js'
WASM = ROOT / 'bench/wasm'
QUICKJS_REVISION = 'e585640ffa2cf88ce7c2d587583c6fe15928ec2f'  # master on 2026-11-11, version 2026-06-04
WASMTIME = 'https://github.com/bytecodealliance/wasmtime/releases/download/v45.0.0/wasmtime-v45.0.0-{arch}-linux.tar.xz'
WASI_SDK = 'https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-33/wasi-sdk-33.0-{arch}-linux.tar.gz'
WASM_STACK = 'max-wasm-stack=8388608'
SETUP_STEPS = ['node', 'jsc', 'octane', 'quickjs', 'wasmtime', 'wasi-sdk', 'wasm']
ENGINES = ['silkjs', 'v8-jitless', 'jsc-nojit', 'qjs', 'silkjs-wasm', 'qjs-wasm']
JIT_ENGINES = ['v8-jit', 'jsc-jit']  # context only
# jsc's shell has preciseTime() (seconds). Its `performance` is missing or, in some builds, a stub whose
# now() returns 0; without a clock microbench.js falls back to Date.now. Some builds have no `console`.
JSC_CLOCK = ('globalThis.performance = { now: function () { return preciseTime() * 1000; } };\n'
             'if (typeof console === "undefined") globalThis.console = { log: print };\n')
# Iterations of each fixed-work script, as scripts/v8bench.py --instructions runs them.
FIXED = {'richards': 20, 'deltablue': 20, 'crypto': 4, 'raytrace': 8, 'earley-boyer': 2, 'regexp': 2,
         'splay': 4, 'navier-stokes': 2}


def find_node(given=None):
    node = benchenv.executable(given, 'NODE_REFERENCE', 'node', 'node',
                               'Install Node 24 or set NODE_REFERENCE (the tables use Node 24 for V8).')
    version = subprocess.run([node, '--version'], env=benchenv.clean_environment(), capture_output=True, text=True).stdout.strip()
    return node, version


def find_jsc():
    """The JavaScriptCore shell: on PATH, or where the WebKitGTK packages put it."""
    found = shutil.which(os.environ.get('JSC') or 'jsc')
    if found:
        return found
    for candidate in sorted(glob.glob('/usr/lib/*/webkit*gtk-*/jsc') + glob.glob('/usr/lib/*/jsc')):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class Engines:
    """The command of each engine column, and why a column cannot run."""

    def __init__(self, args):
        self.work = benchenv.work_directory(args.work, ROOT / 'build/bench')
        self.bin = self.work / 'bin'
        self.bin.mkdir(exist_ok=True)
        self.environment = benchenv.clean_environment({'OCTANE_DIR': str(self.work / 'octane')})
        self.silkjs = getattr(args, 'engine', None) or os.environ.get('SILKJS_ENGINE') or str(release_binary())
        self.node_given = getattr(args, 'node', None)
        self.wasmtime = self.work / 'tools/wasmtime/wasmtime'
        self.qjs = self.work / 'quickjs/qjs'

    def wrapper(self, name, line):
        """An executable in <work>/bin that takes the script as its argument."""
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + line + '\n')
        path.chmod(0o755)
        return [str(path)]

    def wasm(self, name, module):
        precompiled = self.work / 'wasm' / f'{module}.cwasm'
        if not self.wasmtime.is_file() or not precompiled.is_file():
            return None, f'{precompiled} or wasmtime is missing; run `silk task bench-setup wasmtime wasi-sdk quickjs wasm`'
        # WASI has no working directory: the script path is made absolute and the filesystem preopened.
        return self.wrapper(name, f'f=$(realpath "$1"); shift\nexec {shlex.quote(str(self.wasmtime))} run --allow-precompiled '
                                  f'--dir / -W {WASM_STACK} {shlex.quote(str(precompiled))} "$f" "$@"'), None

    def command(self, name, micro=False):
        """(argv, None) for an engine, or (None, why it is unavailable). `micro` gives the microbench.js command."""
        script = [str(MICRO)] if micro else []
        if name == 'silkjs':
            if not os.access(self.silkjs, os.X_OK):
                return None, f'{self.silkjs} is missing; run `silk build --release`, or pass --engine / set SILKJS_ENGINE'
            return ['env', 'SILKJS_PREPARSE_THREADS=0', str(Path(self.silkjs).resolve())] + script, None
        if name in ('v8-jitless', 'v8-jit'):
            try:
                node, _ = find_node(self.node_given)
            except SystemExit as error:
                return None, str(error)
            flags = ['--jitless'] if name == 'v8-jitless' else []
            if micro:
                return [node, *flags, *script], None  # node directly: no loader between node and microbench.js
            return self.wrapper(name, f'exec {shlex.quote(node)} {" ".join(flags)} {shlex.quote(str(HARNESS / "noderun.js"))} "$@"'), None
        if name in ('jsc-nojit', 'jsc-jit'):
            jsc = find_jsc()
            if not jsc:
                return None, 'jsc is missing; run `silk task bench-setup jsc`'
            flags = ['--useJIT=false'] if name == 'jsc-nojit' else []
            if micro:
                shimmed = self.work / 'microbench-jsc.js'
                shimmed.write_text(JSC_CLOCK + MICRO.read_text())
                return [jsc, *flags, str(shimmed)], None
            return [jsc, *flags], None
        if name == 'qjs':
            if not self.qjs.is_file():
                return None, f'{self.qjs} is missing; run `silk task bench-setup quickjs`'
            return [str(self.qjs)] + script, None
        if name in ('silkjs-wasm', 'qjs-wasm'):
            argv, why = self.wasm(name, name.split('-')[0])
            return (argv + script if argv else None), why
        return None, f'unknown engine; the engines are {", ".join(ENGINES + JIT_ENGINES)}'

    def columns(self, args, micro=False):
        """name=command for each chosen engine. Named engines must exist; the default set skips missing ones."""
        chosen = args.engines.split(',') if args.engines else ENGINES + (JIT_ENGINES if args.jit else [])
        columns, shown = [], {}
        for name in chosen:
            argv, why = self.command(name, micro)
            if argv is None:
                if args.engines or name == 'silkjs':
                    fail(f'{name}: {why}')
                shown[name] = 'SKIPPED, ' + why
                continue
            columns.append((name, argv))
            shown[name] = shlex.join(argv)
        return columns, shown


def results(args, engines):
    out = Path(args.out).expanduser().resolve() if args.out else engines.work / 'results'
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------- setup

def setup(args):
    engines = Engines(args)
    work = engines.work
    tools = work / 'tools'
    downloads = work / 'downloads'
    for directory in (tools, downloads, work / 'wasm'):
        directory.mkdir(exist_ok=True)
    arch = os.uname().machine
    steps = benchenv.Steps(args.names, SETUP_STEPS)

    def node():
        path, version = find_node(args.node)
        note = f'{path} {version}'
        return note if version.startswith('v24.') else note + ' (WARNING: the tables use Node 24; set NODE_REFERENCE)'

    def jsc():
        if not find_jsc():
            benchenv.apt_install(['libjavascriptcoregtk-bin', 'libjavascriptcoregtk-6.0-bin', 'libjavascriptcoregtk-4.1-bin'])
        found = find_jsc()
        if not found:
            raise RuntimeError('no jsc after installing the JavaScriptCore shell package; install it and set JSC')
        return found

    def octane():
        checkout = work / 'octane'
        if not (checkout / 'base.js').is_file():
            sh(['git', 'clone', '-q', '--depth', '1', 'https://github.com/chromium/octane', checkout])
        return subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', '--short', 'HEAD'], text=True).strip()

    def quickjs():
        checkout = work / 'quickjs'
        if not (checkout / '.git').is_dir():
            sh(['git', 'clone', '-q', 'https://github.com/bellard/quickjs', checkout])
        sh(['git', '-C', checkout, 'checkout', '-q', QUICKJS_REVISION])
        if not engines.qjs.is_file():
            sh(['make', '-C', checkout, f'-j{os.cpu_count() or 2}', 'qjs'], stdout=subprocess.DEVNULL)
        return f'{engines.qjs} at {QUICKJS_REVISION[:7]}'

    def unpack(url, name, flag, marker):
        target = tools / name
        if (target / marker).is_file():
            return str(target)
        archive = downloads / url.rsplit('/', 1)[1]
        benchenv.download(url, archive)
        target.mkdir(exist_ok=True)
        sh(['tar', flag, archive, '-C', target, '--strip-components', '1'])
        return str(target)

    def wasmtime():
        return unpack(WASMTIME.format(arch=arch), 'wasmtime', 'xJf', 'wasmtime')

    def wasi_sdk():
        return unpack(WASI_SDK.format(arch=arch), 'wasi-sdk', 'xzf', 'bin/clang')

    def wasm():
        """silkjs.wasm and qjs.wasm, precompiled by wasmtime with the stack setting they run with."""
        if benchenv.running_builds():
            raise RuntimeError('another build is running')
        if not shutil.which('silk'):
            raise RuntimeError('silk is not on PATH (silkjs.wasm is built by bench/wasm/build.sh)')
        clang = os.environ.get('SILK_CLANG', 'clang')
        linker = Path(shutil.which(clang) or clang).parent / 'wasm-ld'
        if not linker.is_file():
            raise RuntimeError(f'no wasm-ld beside {clang}; set SILK_CLANG to a Clang 22 or newer that has one')
        if not engines.wasmtime.is_file() or not (tools / 'wasi-sdk/bin/clang').is_file() or not (work / 'quickjs/quickjs.c').is_file():
            raise RuntimeError('run the wasmtime, wasi-sdk and quickjs steps first')
        environment = dict(engines.environment, WASI_SDK=str(tools / 'wasi-sdk'))
        sh([WASM / 'build.sh'], env=environment)
        sh([WASM / 'quickjs/build.sh', work / 'quickjs', WASM / 'build/qjs.wasm'], env=environment)
        for module in ('silkjs', 'qjs'):
            sh([engines.wasmtime, 'compile', '-W', WASM_STACK, WASM / f'build/{module}.wasm', '-o', work / f'wasm/{module}.cwasm'])
        return str(work / 'wasm')

    for name, action in (('node', node), ('jsc', jsc), ('octane', octane), ('quickjs', quickjs), ('wasmtime', wasmtime),
                         ('wasi-sdk', wasi_sdk), ('wasm', wasm)):
        steps.step(name, action)
    print(f'\nwork directory: {work}')
    steps.finish()


# ---------------------------------------------------------------- the runs

def octane(args):
    engines = Engines(args)
    benchenv.check_rounds(args.rounds, 3)
    if not (engines.work / 'octane/base.js').is_file():
        fail(f'no Octane checkout in {engines.work}; run `silk task bench-setup octane`')
    columns, shown = engines.columns(args)
    benchenv.refuse_while_building(args.force)
    out = results(args, engines)
    benchenv.announce('Octane, best score per engine (bench/engines/octane.py)',
                      {'rounds': args.rounds, 'benchmarks': ' '.join(args.names) or 'all', **shown, 'out': out})
    command = [sys.executable, '-u', HARNESS / 'octane.py', args.rounds, out / 'octane.json']
    command += [f'{name}={shlex.join(argv)}' for name, argv in columns]
    if args.names:
        command += ['--only', *args.names]
    status = benchenv.run(command, cwd=engines.work, env=engines.environment, log=out / 'octane.log')
    print(f'scores: {out / "octane.json"}; Octane score mode is too noisy to compare two builds, use `silk task bench-fixed`')
    sys.exit(status)


def micro(args):
    engines = Engines(args)
    benchenv.check_rounds(args.rounds, 3)
    columns, shown = engines.columns(args, micro=True)
    benchenv.refuse_while_building(args.force)
    out = results(args, engines)
    benchenv.announce('micro benchmarks, best ns per test (bench/engines/micro.py)',
                      {'rounds': args.rounds, 'tests': ' '.join(args.names) or 'all', **shown, 'out': out})
    command = [sys.executable, '-u', HARNESS / 'micro.py', out / 'micro.json', args.rounds]
    for name, argv in columns:
        # Test names follow the script. jsc's shell wants `--` before a script's arguments and does not
        # pass the script itself, which microbench.js expects first.
        names = (['--', 'microbench.js'] if name.startswith('jsc') and args.names else []) + args.names
        command.append(f'{name}={shlex.join(argv + names)}')
    status = benchenv.run(command, cwd=engines.work, env=engines.environment, log=out / 'micro.log')
    print(f'times: {out / "micro.json"}')
    sys.exit(status)


def fixed(args):
    """Fixed work for comparing two builds: the same iterations under each binary, minimum user time."""
    engines = Engines(args)
    benchenv.check_rounds(args.rounds, 5)
    source = engines.work / 'octane'
    if not (source / 'base.js').is_file():
        fail(f'no Octane checkout in {engines.work}; run `silk task bench-setup octane`')
    if not args.only:
        fail('name the benchmarks: --only richards,deltablue')
    binaries = []
    for binary in args.names:
        found = shutil.which(binary) or binary
        if not os.access(found, os.X_OK):
            fail(f'{binary} is not an executable')
        binaries.append(str(Path(found).resolve()))
    if len(binaries) < 2:
        fail('give at least two binaries to compare: OLD NEW')
    os.environ['OCTANE_DIR'] = str(source)
    sys.path.insert(0, str(HARNESS))
    import octane as octane_harness
    groups = dict(octane_harness.GROUPS)
    folder = engines.work / 'fx'
    folder.mkdir(exist_ok=True)
    names = args.only.split(',')
    for name in names:
        if name not in groups:
            fail(f'unknown benchmark {name}; the benchmarks are {", ".join(groups)}')
        count = args.iterations or FIXED.get(name)
        if not count:
            fail(f'{name} has no default iteration count; pass --iterations N (aim for about a second per run)')
        # The recipe in fx.py's header.
        text = "if (typeof read === 'undefined') var read = function(){ throw new Error('no read'); };\n"
        for part in ['base'] + groups[name]:
            text += (source / f'{part}.js').read_text(encoding='utf-8', errors='surrogateescape') + '\n'
        text += ('for (var s=0;s<BenchmarkSuite.suites.length;s++){var b=BenchmarkSuite.suites[s].benchmarks[0]; '
                 f'BenchmarkSuite.ResetRNG(); if (b.Setup) b.Setup(); for (var k=0;k<{count};k++) b.run(); }}\n')
        (folder / f'fx-{name}.js').write_text(text, encoding='utf-8', errors='surrogateescape')
    benchenv.refuse_while_building(args.force)
    benchenv.announce('fixed work, minimum user time (bench/engines/fx.py); under 2% is noise',
                      {'rounds': args.rounds, 'benchmarks': args.only, 'binaries': ' '.join(binaries)})
    sys.exit(benchenv.run([sys.executable, '-u', HARNESS / 'fx.py', args.rounds, args.only, *binaries],
                          cwd=folder, env=engines.environment))


def report(args):
    engines = Engines(args)
    out = results(args, engines)
    octane_json, micro_json = out / 'octane.json', out / 'micro.json'
    if not octane_json.is_file():
        fail(f'{octane_json} is missing; run `silk task bench-octane` first (or pass --out)')
    if args.native:
        if not micro_json.is_file():
            fail(f'{micro_json} is missing; the native table needs `silk task bench-micro` too')
        command = [HARNESS / 'rep.py', octane_json, micro_json] + ([args.jit_results] if args.jit_results else [])
    else:
        command = [HARNESS / 'rep-wasm.py', octane_json] + ([micro_json] if micro_json.is_file() else [])
    sys.exit(benchenv.run([sys.executable, *command], log=out / ('report-native.txt' if args.native else 'report.txt')))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    def command(name, function, names_help, rounds=None, measures=True):
        sub = commands.add_parser(name)
        sub.set_defaults(function=function)
        sub.add_argument('--work', help='work directory (default $SILKJS_BENCH_WORK, else build/bench)')
        sub.add_argument('--node', help='the Node executable for the V8 columns (default $NODE_REFERENCE, else node on PATH)')
        if names_help:
            sub.add_argument('names', nargs='*', help=names_help)
        if measures:
            sub.add_argument('--rounds', type=int, default=rounds, help=f'interleaved rounds (default {rounds})')
            sub.add_argument('--engine', help="the silkjs executable (default $SILKJS_ENGINE, else this host's release build)")
            sub.add_argument('--engines', help='comma-separated columns (default: every installed one of ' + ','.join(ENGINES) + ')')
            sub.add_argument('--jit', action='store_true', help='add the v8-jit and jsc-jit columns, for context only')
            sub.add_argument('--out', help='output directory (default <work>/results)')
            sub.add_argument('--force', action='store_true', help='measure even while a build runs or the host is busy')
        return sub

    command('setup', setup, 'steps to run (default all): ' + ' '.join(SETUP_STEPS), measures=False)
    command('octane', octane, 'benchmarks, such as richards splay (default all)', rounds=3)
    command('micro', micro, 'microbench.js test names (default all)', rounds=3)
    fx = command('fixed', fixed, 'the binaries to compare: OLD NEW...', rounds=7)
    fx.add_argument('--only', help='comma-separated Octane benchmarks, such as richards,deltablue')
    fx.add_argument('--iterations', type=int, help='iterations of each benchmark (default: as scripts/v8bench.py)')
    rep = command('report', report, None, measures=False)
    rep.add_argument('--out', help='the directory with octane.json and micro.json (default <work>/results)')
    rep.add_argument('--native', action='store_true', help='the native-only table (rep.py) instead of the one with wasm columns')
    rep.add_argument('--jit-results', help='an octane.json with v8-jit and jsc-jit columns, for the native table')
    args = parser.parse_args()
    args.function(args)


if __name__ == '__main__':
    main()
