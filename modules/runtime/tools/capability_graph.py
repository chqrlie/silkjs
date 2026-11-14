#!/usr/bin/env python3
"""Step 0 of the composable runtime (docs/composable-runtime.md): the data that fixes service
boundaries before any code moves.

Writes docs/capability-graph.md and docs/capability-graph.json from the sources alone; nothing is
built or run except the vendored acorn, under Node, to parse the built-in JavaScript:

  - every built-in's require and import strings, load-time or lazy, and the `host` natives it reads
  - which proposed service owns each built-in, native function and Silk module (the rules below)
  - the edges that cross a service line without a declared need, which is the JavaScript work
  - bytes of built-in JavaScript, Silk and `extern "C"` declarations per service

The linker-map half of step 0 (bytes of text and data per archive and per `ext` module) needs a
build; `--link-map PATH` adds it when a map from `silk build` is at hand.

  python3 tools/capability_graph.py            # writes the report
  python3 tools/capability_graph.py --check    # fails when the report is out of date
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from embed import sources  # noqa: E402

DOCS = ROOT / 'docs'
REPORT = DOCS / 'capability-graph.md'
DATA = DOCS / 'capability-graph.json'

CORE = 'Core'
EXECUTABLE = 'Executable'
# The closed list of docs/composable-runtime.md, in its order. Core and Executable are not
# services: Core is always linked, Executable is what only the silkjsruntime binary carries.
SERVICES = ['Platform', 'ModuleResolver', 'Files', 'Network', 'Processes', 'SystemInfo', 'Workers',
            'Crypto', 'Tls', 'Compression', 'Http', 'Http2', 'Wasm', 'Database', 'TypeStripping',
            'Inspector', 'NativeAddons', 'HostApi']
ORDER = [CORE] + SERVICES + [EXECUTABLE]

# What each service may name, as the design's "Services that need each other" states it.
# embed.start checks these pairs; anything else that crosses a line is reported below.
NEEDS = {
    'Network': {'Http'},          # http and fetch stand on the parser
    'Tls': {'Network', 'Crypto'},
    'Http2': {'Network', 'Tls'},  # http2.connect('https:') and secure servers
    'Inspector': {'Network'},
    'TypeStripping': {'Wasm'},
    'Database': {'Files'},
    'Wasm': {'Files'},            # wasi preopens; WebAssembly itself needs nothing
    'Processes': {'Network'},     # child stdio and IPC are sockets
    'Workers': set(),
    EXECUTABLE: set(SERVICES),    # the executable provides every real implementation
}

# Built-in JavaScript, by name. First match wins.
BUILTIN_RULES = [
    (r'^(internal/deps/amaro(/.*)?|internal/modules/typescript)$', 'TypeStripping'),
    (r'^(wasi|internal/web/wasm(/.*)?)$', 'Wasm'),
    (r'^(sqlite|internal/sqlite/.*|internal/web/storage|internal/inspector_dom_storage)$', 'Database'),
    (r'^(internal/napi)$', 'NativeAddons'),
    (r'^(http2|internal/http2(/.*)?|internal/inspector/network_http2)$', 'Http2'),
    (r'^(internal/http/native-parser|_http_common)$', 'Http'),
    (r'^(tls|https|_tls_common|_tls_wrap|internal/tls/.*)$', 'Tls'),
    (r'^(crypto|internal/crypto(/.*)?|internal/crypto_.*)$', 'Crypto'),
    (r'^(zlib(/.*)?|internal/compression/.*|internal/webstreams/compression|internal/web/compression)$', 'Compression'),
    (r'^(inspector(/.*)?|internal/inspector.*|internal/inspector/.*|trace_events|internal/trace_events.*'
     r'|internal/profiler_binding|internal/heap_prof|internal/worker/profiles)$', 'Inspector'),
    (r'^(worker_threads|internal/worker|internal/worker/(io|locks|broadcast)|internal/process/worker_thread_only)$', 'Workers'),
    (r'^(child_process|internal/child_process(/.*)?|cluster|internal/cluster/.*)$', 'Processes'),
    (r'^(os|internal/os_binding)$', 'SystemInfo'),
    (r'^(fs(/.*)?|internal/fs(/.*)?|internal/fs_async|internal/path_events/glob)$', 'Files'),
    (r'^(net|dgram|dns(/.*)?|internal/dns/.*|internal/net(/.*)?|internal/dgram|internal/socketaddress'
     r'|internal/socket_list|internal/blocklist|internal/block_list_binding|internal/stream_base_commons'
     r'|internal/js_stream_socket|_http_(agent|client|incoming|outgoing|server)|http|internal/http'
     r'|internal/web/(fetch|websocket|eventsource|serve)(/.*)?|internal/ws|internal/deps/undici(/.*)?'
     r'|internal/inspector/network_(http|undici))$', 'Network'),
    (r'^(internal/loader/.*|internal/modules/(cjs|esm)/.*|internal/modules/(helpers|package_json_reader'
     r'|package_map|customization_hooks)|internal/modules_binding)$', 'ModuleResolver'),
    (r'^(repl|internal/repl(/.*)?|internal/main/.*|test(/.*)?|internal/test_runner/.*|internal/test/.*'
     r'|internal/watch_mode/.*|sea|internal/sea|internal/debugger/.*|internal/postgres_rows'
     r'|internal/options_table)$', EXECUTABLE),
]
# Ordering above matters for the two inspector network files, which go with what they trace.
BUILTIN_RULES.sort(key=lambda rule: 0 if 'network_' in rule[0] and rule[1] != 'Inspector' else 1)

# Silk modules under src/, by file name. First match wins.
SILK_RULES = [
    (r'^ext/(wasm_|wasi_|wamr).*', 'Wasm'),
    (r'^ext/(sqlite_native|webstorage_native)', 'Database'),
    (r'^ext/napi_', 'NativeAddons'),
    (r'^ext/http2_', 'Http2'),
    (r'^ext/http_parser_', 'Http'),
    (r'^ext/(tls_|openssl_x509)', 'Tls'),
    (r'^ext/(crypto_native|openssl)', 'Crypto'),
    (r'^ext/compression_', 'Compression'),
    (r'^ext/(inspector_|profile_|coverage_native|trace_|sigint_trace|report_json|resource_snapshot)', 'Inspector'),
    (r'^ext/worker_', 'Workers'),
    (r'^ext/(process_native|process_abi|spawn_sync)', 'Processes'),
    (r'^ext/os_', 'SystemInfo'),
    (r'^ext/(fs_native|fs_ops|fs_descriptors|uv_fs)', 'Files'),
    (r'^ext/(net_native|dns_|uv_net)', 'Network'),
    (r'^ext/(sea_|heap_image)', EXECUTABLE),
    (r'^(main|tasks)\.silk$', EXECUTABLE),
]

# Id blocks of native functions (src/libs/core/host.silk nativeGeneric). The design keeps these
# as one registry in the core; a block belongs to a service.
ID_BLOCKS = [
    (1, 999, CORE), (1000, 1999, CORE),
    (2000, 2007, 'Files'),            # fs, watch, and pollIO/hasPendingIO, which the loop shares
    (2100, 2101, 'Crypto'),
    (2200, 2299, CORE),               # async context, ticks, immediates
    (2300, 2300, 'SystemInfo'),
    (2400, 2405, 'Network'),
    (2500, 2500, 'Tls'),
    (2600, 2601, 'Compression'),
    (2700, 2700, 'Processes'),        # process_native: child processes and process.* together
    (2800, 2899, CORE),               # url
    (3000, 3999, CORE),               # vm
    (5000, 5000, 'Http2'),
    (6000, 6099, 'Workers'),
    (7000, 7000, 'Database'),
    (9000, 9000, 'Wasm'), (10000, 10000, 'Wasm'),
    (11000, 11000, 'Http'),
    (12000, 12000, 'Inspector'),
    (13000, 13000, EXECUTABLE),
    (14000, 14000, 'NativeAddons'),
    (15000, 15999, 'Inspector'), (16000, 16000, 'Inspector'),
    (17000, 17000, CORE),             # permission gate
    (18000, 18999, 'Inspector'),
]
# Modules bootstrap.js defines in code rather than from a file: the `host` object every built-in
# reads natives from, and the engine's libc modules it spreads into host.rawStd and host.rawOS.
# Their uses are counted as native reads, not requires.
DEFINED_BY_BOOTSTRAP = {'internal/host', 'silk:host/std', 'silk:host/os'}
# Data properties of __silkNative and what bootstrap.js adds to host that carry a capability.
NATIVE_DATA = {'fsConstants': 'Files', 'rawOS': 'Platform', 'rawStd': 'Platform'}


def classify(name: str, rules) -> str:
    for pattern, owner in rules:
        if re.match(pattern, name):
            return owner
    return CORE


def block_owner(native_id: int) -> str:
    for low, high, owner in ID_BLOCKS:
        if low <= native_id <= high:
            return owner
    return CORE


def native_names() -> dict[str, int]:
    """Name to id of every function worker.silk configure defines on __silkNative."""
    worker = (ROOT / 'src/runtime/worker.silk').read_text()
    start = worker.index('unsafe effect fn configure')
    body = worker[start:worker.index('let result = (run evaluation.evaluate', start)]
    constants = {'url_native.BASE': 2800, 'inspector_native.BASE': 15000, 'profile_native.BASE': 18000}
    for module, symbol in (('url_native', 'BASE'), ('inspector_native', 'BASE'), ('profile_native', 'BASE')):
        text = (ROOT / f'src/ext/{module}.silk').read_text()
        found = re.search(rf'pub const {symbol}\s*:\s*u32\s*=\s*(\d+)', text)
        if found:
            constants[f'{module}.{symbol}'] = int(found.group(1))
    names: dict[str, int] = {}
    for name, number in re.findall(r'function\(&mut engine, context, native, b"(\w+)", (\d+)\)', body):
        names[name] = int(number)
    # `let xNames = [b"a", b"b"]` followed by a loop with `xNames[at], BASE + ...`.
    for listing, items in re.findall(r'let (\w+) = \[(.*?)\]', body, re.S):
        used = re.search(rf'{listing}\[at\], ([\w.]+) \+', body)
        if not used:
            continue
        base = constants.get(used.group(1)) if not used.group(1).isdigit() else int(used.group(1))
        if base is None:
            continue
        offset = 1 if used.group(1) == 'usize.toU32(at)' else 0
        for index, name in enumerate(re.findall(r'b"(\w+)"', items)):
            names[name] = base + index + offset
    # The first list is numbered from 1 (`usize.toU32(at) + 1`).
    first = re.search(r'let names = \[(.*?)\]', body, re.S)
    if first:
        for index, name in enumerate(re.findall(r'b"(\w+)"', first.group(1))):
            names[name] = index + 1
    for module, base in (('profile_native', constants['profile_native.BASE']), ('vm_native', 3000), ('introspection', 1000)):
        text = (ROOT / f'src/ext/{module}.silk').read_text()
        function = re.search(r'pub fn name\(operation: u32\).*?\n}', text, re.S)
        if function:
            for number, name in re.findall(r'(\d+)\s*=>\s*b"(\w+)"', function.group(0)):
                names[name] = base + int(number)
            if not re.search(r'\d+\s*=>\s*b"', function.group(0)):
                for index, name in enumerate(re.findall(r'b"(\w+)"', function.group(0))):
                    names[name] = base + index
    return names


def extract(entries) -> dict:
    node = shutil.which('node') or shutil.which('bun')
    if not node:
        raise SystemExit('capability_graph: needs node (or bun) to run the vendored acorn')
    listing = json.dumps(entries)
    result = subprocess.run([node, str(ROOT / 'tools/capability_graph_extract.js')], input=listing,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def resolve(specifier: str, parent: str, known: set[str]) -> str | None:
    """The built-in a require string names, as bootstrap.js internalRequire resolves it."""
    if specifier.startswith('node:'):
        specifier = specifier[5:]
    if specifier.startswith('.'):
        base = parent.rsplit('/', 1)[0] if '/' in parent else ''
        for part in specifier.split('/'):
            if part == '..':
                base = base.rsplit('/', 1)[0] if '/' in base else ''
            elif part not in ('.', ''):
                base = f'{base}/{part}' if base else part
        specifier = base
    if specifier.endswith('.js'):
        specifier = specifier[:-3]
    return specifier if specifier in known else None


def silk_inventory():
    modules = {}
    for path in sorted((ROOT / 'src').rglob('*')):
        if path.suffix not in ('.silk', '.c', '.h') or not path.is_file():
            continue
        relative = path.relative_to(ROOT / 'src').as_posix()
        text = path.read_text(errors='replace')
        modules[relative] = {
            'service': classify(relative, SILK_RULES),
            'bytes': len(text.encode()),
            'externC': len(re.findall(r'extern "C" fn', text)),
            'imports': sorted(set(re.findall(r'^import\s+([\w.]+)', text, re.M))),
        }
    return modules


def build(link_map: Path | None):
    entries = [{'name': name, 'path': str(path)} for name, path in sources()]
    entries.append({'name': 'internal/bootstrap/silk', 'path': str(ROOT / 'runtime/bootstrap.js'), 'module': True})
    known = {entry['name'] for entry in entries}
    facts = extract(entries)
    ids = native_names()
    native_service = {name: block_owner(number) for name, number in ids.items()}
    native_service.update(NATIVE_DATA)

    builtins = {}
    for entry in entries:
        name = entry['name']
        fact = facts[name]
        owner = classify(name, BUILTIN_RULES) if name != 'internal/bootstrap/silk' else CORE
        requires = {}
        unresolved = []
        for specifier, when in fact['requires'].items():
            if specifier in DEFINED_BY_BOOTSTRAP:
                continue
            target = resolve(specifier, name, known)
            if target is None:
                unresolved.append(specifier)
            else:
                requires[target] = when if requires.get(target) != 'load' else 'load'
        builtins[name] = {
            'service': owner,
            'bytes': Path(entry['path']).stat().st_size,
            'requires': dict(sorted(requires.items())),
            'unresolved': unresolved,
            'dynamicRequires': fact['dynamic'],
            'natives': [n for n in fact['natives'] if n in native_service],
            'parseError': fact.get('error'),
        }

    def allowed(source: str, target: str) -> bool:
        if source == target or target == CORE:
            return True
        if target in ('Platform', 'ModuleResolver'):
            return True  # always real
        return target in NEEDS.get(source, set())

    crossings = []
    for name, built in builtins.items():
        for target, when in built['requires'].items():
            target_service = builtins[target]['service']
            if not allowed(built['service'], target_service):
                crossings.append({'from': name, 'fromService': built['service'], 'to': target,
                                  'toService': target_service, 'kind': 'require', 'when': when})
        for native in built['natives']:
            target_service = native_service[native]
            if not allowed(built['service'], target_service):
                crossings.append({'from': name, 'fromService': built['service'], 'to': 'host.' + native,
                                  'toService': target_service, 'kind': 'native', 'when': 'call'})

    silk = silk_inventory()
    sizes = None
    if link_map is not None:
        sizes = link_map_sizes(link_map)
    return {
        'builtins': builtins,
        'natives': {name: {'id': ids.get(name), 'service': service} for name, service in sorted(native_service.items())},
        'crossings': crossings,
        'silk': silk,
        'linkMap': sizes,
    }


def link_map_sizes(path: Path):
    """Bytes per archive or object from a GNU ld or lld map (`-Wl,-Map=...`)."""
    sizes: Counter = Counter()
    text = path.read_text(errors='replace')
    # lld: "     VMA      LMA     Size Align Out     In      Symbol" rows with hex size, then the input.
    for match in re.finditer(r'^\s*[0-9a-f]+\s+[0-9a-f]+\s+([0-9a-f]+)\s+\d+\s+(\S+\.(?:a|o)(?:\(\S+\))?):\(\.(?:text|data|rodata|bss)', text, re.M):
        archive = re.sub(r'\(.*\)$', '', match.group(2))
        sizes[Path(archive).name] += int(match.group(1), 16)
    # GNU ld: " .text  0x...  0x<size> <input>".
    for match in re.finditer(r'^\s\.(?:text|data|rodata|bss)\S*\s+0x[0-9a-f]+\s+0x([0-9a-f]+)\s+(\S+)', text, re.M):
        archive = re.sub(r'\(.*\)$', '', match.group(2))
        sizes[Path(archive).name] += int(match.group(1), 16)
    return dict(sizes.most_common())


def kib(size: int) -> str:
    return f'{size / 1024:,.1f}'


def render(data) -> str:
    builtins = data['builtins']
    lines = [
        '# Capability graph',
        '',
        'Generated by `tools/capability_graph.py` (step 0 of [the composable runtime](composable-runtime.md)).',
        'Do not edit by hand; run `python3 tools/capability_graph.py`. Ownership of each built-in, native',
        'function and Silk module is a first cut by name (the rules are in the tool); moving a file between',
        'services is a one-line rule change, and this report is what to read before doing it.',
        '',
        '## Size by service',
        '',
        'Built-in JavaScript is source bytes (before bytecode). Silk is source bytes of `src/`, a proxy for',
        'code size until the linker map is read. `extern \"C\"` counts declarations, the libc and library',
        'surface each service reaches today.',
        '',
        '| Service | Built-ins | JavaScript KiB | Load-time requires | Lazy requires | Silk modules | Silk KiB | `extern "C"` |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    by_service = defaultdict(lambda: Counter())
    for built in builtins.values():
        row = by_service[built['service']]
        row['builtins'] += 1
        row['js'] += built['bytes']
        row['load'] += sum(1 for when in built['requires'].values() if when == 'load')
        row['lazy'] += sum(1 for when in built['requires'].values() if when == 'lazy')
    for module in data['silk'].values():
        row = by_service[module['service']]
        row['silk'] += 1
        row['silkBytes'] += module['bytes']
        row['extern'] += module['externC']
    total = Counter()
    for service in ORDER:
        row = by_service.get(service)
        if not row:
            continue
        total.update(row)
        lines.append(f"| {service} | {row['builtins']} | {kib(row['js'])} | {row['load']} | {row['lazy']} | {row['silk']} | {kib(row['silkBytes'])} | {row['extern']} |")
    lines.append(f"| **Total** | {total['builtins']} | {kib(total['js'])} | {total['load']} | {total['lazy']} | {total['silk']} | {kib(total['silkBytes'])} | {total['extern']} |")
    lines += ['']

    if data['linkMap']:
        lines += ['## Linked bytes per archive', '', '| Archive or object | KiB |', '| --- | ---: |']
        for name, size in list(data['linkMap'].items())[:60]:
            lines.append(f'| `{name}` | {kib(size)} |')
        lines.append('')
    else:
        lines += ['The linked size per archive and per `ext` module is not here yet: it needs a build',
                  '(`silk build` with a linker map, then `--link-map PATH`).', '']

    largest = sorted(builtins.items(), key=lambda item: -item[1]['bytes'])[:12]
    lines += ['### Largest built-ins', '', '| Built-in | Service | KiB |', '| --- | --- | ---: |']
    for name, built in largest:
        lines.append(f"| `{name}` | {built['service']} | {kib(built['bytes'])} |")
    lines.append('')

    crossings = data['crossings']
    lines += [
        '## Edges across service lines',
        '',
        'A require or native call from one service into another that is not among its declared needs',
        '(`NEEDS` in the tool, the pairs `embed.start` will check). Each is a decision for step 2: move the',
        'piece to the core, turn it into a registration the capability makes when it loads, or declare the',
        'need. Load-time edges block an empty service outright; lazy ones fail only on the path that takes',
        'them.',
        '',
    ]
    pairs = Counter((c['fromService'], c['toService']) for c in crossings)
    lines += ['| From | To | Edges | Load-time |', '| --- | --- | ---: | ---: |']
    for (source, target), count in sorted(pairs.items(), key=lambda item: (-item[1], item[0])):
        load = sum(1 for c in crossings if c['fromService'] == source and c['toService'] == target and c['when'] == 'load')
        lines.append(f'| {source} | {target} | {count} | {load} |')
    lines.append('')
    lines += ['### Core built-ins that reach a capability', '',
              'The second case of the design: a core module using a capability for one feature.', '',
              '| Built-in | Reaches | When |', '| --- | --- | --- |']
    for c in sorted((c for c in crossings if c['fromService'] == CORE), key=lambda c: (c['when'] != 'load', c['from'], c['to'])):
        lines.append(f"| `{c['from']}` | `{c['to']}` ({c['toService']}) | {c['when']} |")
    lines.append('')
    lines += ['### Every other crossing', '', '| Built-in | Service | Reaches | Service | When |', '| --- | --- | --- | --- | --- |']
    for c in sorted((c for c in crossings if c['fromService'] != CORE), key=lambda c: (c['fromService'], c['from'], c['to'])):
        lines.append(f"| `{c['from']}` | {c['fromService']} | `{c['to']}` | {c['toService']} | {c['when']} |")
    lines.append('')

    lines += ['## Native functions by service', '', '| Service | Natives |', '| --- | --- |']
    natives = defaultdict(list)
    for name, native in data['natives'].items():
        natives[native['service']].append(name)
    for service in ORDER:
        if natives.get(service):
            listed = ', '.join(f'`{n}`' for n in sorted(natives[service])[:40])
            more = len(natives[service]) - 40
            lines.append(f"| {service} | {listed}{f' and {more} more' if more > 0 else ''} |")
    lines.append('')

    lines += ['## Silk modules by service', '', '| Service | Modules |', '| --- | --- |']
    silk = defaultdict(list)
    for name, module in data['silk'].items():
        if name.endswith('.silk'):
            silk[module['service']].append(name)
    for service in ORDER:
        if silk.get(service):
            lines.append(f"| {service} | {', '.join(f'`{n}`' for n in sorted(silk[service]))} |")
    lines.append('')

    unresolved = sorted({(name, s) for name, b in builtins.items() for s in b['unresolved']})
    errors = sorted((name, b['parseError']) for name, b in builtins.items() if b['parseError'])
    dynamic = sorted((name, b['dynamicRequires']) for name, b in builtins.items()
                     if b['dynamicRequires']['load'] or b['dynamicRequires']['lazy'])
    lines += ['## Not resolved', '',
              f'{len(unresolved)} require strings name no built-in (besides `internal/host` and the engine\'s',
              f'`silk:host/*` modules). {len(dynamic)} built-ins call require with a computed name, which no',
              'static check can see; those are listed so step 2 reviews them by hand.', '']
    if unresolved:
        lines += ['| Built-in | Names |', '| --- | --- |']
        for name, specifier in unresolved:
            lines.append(f'| `{name}` | `{specifier}` |')
        lines.append('')
    if errors:
        lines += ['Files the parser could not read: ' + ', '.join(f'`{n}` ({e})' for n, e in errors), '']
    if dynamic:
        lines += ['| Built-in | Computed requires (load, lazy) |', '| --- | --- |']
        for name, counts in dynamic:
            lines.append(f"| `{name}` | {counts['load']}, {counts['lazy']} |")
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--check', action='store_true', help='fail when the checked-in report is stale')
    parser.add_argument('--link-map', type=Path, help='a linker map of a silk build, for linked sizes')
    arguments = parser.parse_args()
    data = build(arguments.link_map)
    report = render(data) + '\n'
    encoded = json.dumps(data, indent=1, sort_keys=True) + '\n'
    if arguments.check:
        stale = [p for p, text in ((REPORT, report), (DATA, encoded)) if not p.exists() or p.read_text() != text]
        if stale:
            raise SystemExit('capability_graph: out of date, run python3 tools/capability_graph.py: ' + ', '.join(str(p.relative_to(ROOT)) for p in stale))
        return
    DOCS.mkdir(exist_ok=True)
    REPORT.write_text(report)
    DATA.write_text(encoded)
    print(f'{REPORT.relative_to(ROOT)}: {len(data["builtins"])} built-ins, {len(data["crossings"])} crossings')


if __name__ == '__main__':
    main()
