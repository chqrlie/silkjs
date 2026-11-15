#!/usr/bin/env python3
"""Build the embedding smoke test, a host that embeds the runtime, and check what it prints.

The host evaluates its own module (no script in its arguments), turns the loop with a
bounded wait while the runtime is kept alive with nothing referenced, and the module
ends the runtime with process.exit: the host's turn reports the status, and 'exit'
listeners ran. Run after tools/build.py, which prepares the native archives and
src/runtime/embedded.silk: python3 tests/embed-smoke.py [--skip-build]
"""
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
# The same host (the core's src/embed_smoke.silk) twice: without WebAssembly (./smoke, EmptyWasm),
# and with it (./cli, WamrWasm). Only the second program depends on WAMR.
# The first has no file system either (EmptyFiles); the second's is in memory (MemoryFiles).
# The libraries of capabilities in packages of their own are linked only into the second: these
# are a symbol of each, which the first must not define.
LIBRARIES = {'WAMR': 'wasm_runtime_full_init', 'zlib': 'deflate', 'brotli': 'BrotliEncoderCreateInstance',
             'zstd': 'ZSTD_createCStream', 'nghttp2': 'nghttp2_session_client_new'}
PROGRAMS = [
    ('embed-smoke', ROOT / 'smoke', 'src/main.silk', 'undefined', False),
    ('embed-smoke-wamr', ROOT / 'cli', 'src/embed_smoke_wamr.silk', 'object', True),
]


def defined(executable):
    listing = subprocess.run(['nm', '--defined-only', str(executable)], capture_output=True, text=True, check=True).stdout
    return {line.split()[-1] for line in listing.splitlines() if line.split()}


def expected(wasm, files):
    return [
        'add 5',
        'wasm ' + wasm,
        'lib 42',
        'libraries ' + ('object object function object object object object object function object strip true' if files else 'undefined undefined undefined undefined undefined undefined undefined undefined undefined undefined false false'),
        'dlopen ' + ('ERR_DLOPEN_FAILED' if files else 'ERR_DLOPEN_DISABLED'),
        'env EMBED_SMOKE fixed /embed-smoke',
        'setenv EPERM',
        *(['fs function', 'read hello from memory', 'dir greeting.txt,out.txt 6 false'] if files else
          ['fs ERR_UNKNOWN_BUILTIN_MODULE undefined false']),
        'crypto ' + ('there' if files else 'ERR_NO_CRYPTO'),
        'worker ' + ('started' if files else 'ERR_WORKER_INIT_FAILED'),
        'cjs helpers',
        'missing ERR_MODULE_NOT_FOUND',
        'tick 1',
        'tick 2',
        'tick 3',
        'async ' + ('abcdef' if files else 'none'),
        'exit 3',
        'host: callback returned 42 after its microtask',
        'host: finished with 3',
    ]


def build(name, package, source):
    version = tomllib.loads((ROOT / 'silk.toml').read_text())['package']['silk-version']
    directory = ROOT / 'build' / name
    directory.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'SILKJS_CLANG_BUILD': str(directory)}
    subprocess.run(['silk', '+' + version, 'build', source, '-o', str(directory / name),
                    '--debug', 'true', '--clang', str(ROOT / 'tools/clang.py')],
                   cwd=package, env=environment, check=True)


def main():
    failed = False
    for name, package, source, wasm, files in PROGRAMS:
        if '--skip-build' not in sys.argv[1:]:
            build(name, package, source)
        executable = ROOT / 'build' / name / name
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        lines = result.stdout.splitlines()
        symbols = defined(executable)
        linked = [library for library, symbol in LIBRARIES.items() if symbol in symbols]
        wanted = list(LIBRARIES) if files else []
        if linked != wanted:
            print(f'{name} failed: links {linked or "no library"}, expected {wanted or "none"}')
            failed = True
        if result.returncode != 3 or lines != expected(wasm, files):
            print(f'{name} failed: status {result.returncode}')
            print('stdout:\n' + result.stdout)
            print('stderr:\n' + result.stderr)
            print('expected:\n' + '\n'.join(expected(wasm, files)))
            failed = True
        else:
            print(f'{name} passed')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
