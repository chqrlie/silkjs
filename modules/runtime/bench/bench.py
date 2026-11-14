#!/usr/bin/env python3
"""Interleaved runtime benchmarks (Bun's advertised set) across runtimes.

usage: bench.py ROUNDS OUT.json [--only group,group]     (silk task bench runs this)
Each round runs every (benchmark, runtime) pair once, in a shuffled order, so host noise
spreads over all runtimes. Results are appended per sample; report.py takes medians.
Servers are pinned to CPU 0, load generators/clients to CPUs 2,3.
"""
import json, os, random, re, shutil, signal, subprocess, sys, time, urllib.request, ssl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The work directory `silk task bench-setup` fills: Bun's bench folders, the 1 GB file.
BB = os.path.abspath(os.environ.get('SILKJS_BENCH_WORK') or os.path.join(ROOT, 'build', 'bench'))
B = BB + '/bunrepo/bench'
BIG = BB + '/1g.bin'
RT = os.path.abspath(os.environ.get('RT') or os.environ.get('SILKJS_RUNTIME') or os.path.join(ROOT, 'build', 'silkjsruntime'))
BUN = os.environ.get('BUN') or shutil.which('bun') or 'bun'
# BUN_OPTIONS is dropped on purpose: this container exports BUN_OPTIONS=--smol, which slows Bun ~25%.
# Only the explicit *_smol runtime sets it, to show what earlier (inherited --smol) baselines measured.
ENV0 = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'BUN_OPTIONS')}
ENV0.update(PGUSER='postgres', PGPASSWORD='postgres', PGHOST='localhost', PGDATABASE='postgres',
            DATABASE_URL='postgres://postgres:postgres@localhost:5432/postgres')

RUNTIMES = {
    'bun_nojit': ([BUN], {'BUN_JSC_useJIT': '0'}),
    'bun_nojit_smol': ([BUN], {'BUN_JSC_useJIT': '0', 'BUN_OPTIONS': '--smol'}),
    'silkjs': ([RT], {'SILKJS_PREPARSE_THREADS': '0'}),
}
ALL = [r for r in os.environ.get('RUNTIMES', ','.join(RUNTIMES)).split(',')]
if [r for r in ALL if r not in RUNTIMES]:
    sys.exit('RUNTIMES: unknown runtime(s) %s; known: %s' % ([r for r in ALL if r not in RUNTIMES], ', '.join(RUNTIMES)))
BUNS = [r for r in ALL if r.startswith('bun')]
NODEISH = ALL


def cmd(rt, script, *args):
    argv, env = RUNTIMES[rt]
    # This harness's own scripts are in scripts/; the others are Bun's, named relative to their folder.
    if os.path.exists(os.path.join(HERE, 'scripts', script)):
        script = os.path.join(HERE, 'scripts', script)
    e = dict(ENV0); e.update(env)
    return argv + [script] + list(args), e


def proc_cpu(pid):
    try:
        f = open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()
        return int(f[11]) / 100.0, int(f[12]) / 100.0
    except Exception:
        return 0.0, 0.0


def wait_url(url, tries=100):
    ctx = ssl._create_unverified_context()
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=0.5, context=ctx).read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def kill(p):
    try:
        p.send_signal(signal.SIGKILL)
    except Exception:
        pass
    p.wait()


def http(rt, script, url, cwd=BB):
    argv, env = cmd(rt, script)
    env['PORT'] = re.search(r':(\d+)', url).group(1)
    p = subprocess.Popen(['taskset', '-c', '0'] + argv, cwd=cwd, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_url(url):
            return {'error': 'no response'}
        subprocess.run(['taskset', '-c', '2,3', 'wrk', '-t2', '-c50', '-d2s', url], capture_output=True)
        u0, s0 = proc_cpu(p.pid)
        out = subprocess.run(['taskset', '-c', '2,3', 'wrk', '-t2', '-c50', '-d5s', '-H',
                              'Accept-Encoding: identity', url], capture_output=True, text=True).stdout
        u1, s1 = proc_cpu(p.pid)
        m = re.search(r'Requests/sec:\s+([\d.]+)', out)
        tot = re.search(r'(\d+) requests in', out)
        n = int(tot.group(1)) if tot else 0
        errs = re.search(r'Non-2xx or 3xx responses: (\d+)', out)
        rss = int(re.search(r'VmHWM:\s+(\d+)', open(f'/proc/{p.pid}/status').read()).group(1))
        r = {'value': float(m.group(1)) if m else 0.0, 'rss_mb': rss / 1024}
        if n:
            r['user_us'] = (u1 - u0) * 1e6 / n
            r['sys_us'] = (s1 - s0) * 1e6 / n
        if errs:
            r['non2xx'] = int(errs.group(1))
        return r
    finally:
        kill(p)


def ws(rt, script):
    argv, env = cmd(rt, script)
    env['PORT'] = '4001'
    p = subprocess.Popen(['taskset', '-c', '0'] + argv, cwd=B + '/websocket-server', env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        e = dict(ENV0); e['PORT'] = '4001'
        try:
            out = subprocess.run(['timeout', '30', 'taskset', '-c', '2,3', BUN, 'chat-client.mjs'],
                                 cwd=B + '/websocket-server', env=e, capture_output=True, text=True, timeout=40).stdout
        except subprocess.TimeoutExpired as x:
            out = (x.stdout or b'').decode() if isinstance(x.stdout, bytes) else (x.stdout or '')
        vals = sorted(float(v) for v in re.findall(r'([\d.]+) messages per second', out))
        rss = int(re.search(r'VmHWM:\s+(\d+)', open(f'/proc/{p.pid}/status').read()).group(1))
        if not vals:
            return {'error': 'no rounds', 'rss_mb': rss / 1024}
        return {'value': vals[len(vals) // 2], 'max': vals[-1], 'n': len(vals), 'rss_mb': rss / 1024}
    finally:
        kill(p)


def timed(rt, script, cwd, parse, timeout=180, args=(), stdout_file=None):
    argv, env = cmd(rt, script, *args)
    t = time.perf_counter()
    try:
        out = subprocess.run(argv, cwd=cwd, env=env, capture_output=stdout_file is None,
                             stdout=open(stdout_file, 'w') if stdout_file else None,
                             stderr=subprocess.PIPE if stdout_file else None,
                             text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': f'timeout {timeout}s'}
    wall = (time.perf_counter() - t) * 1000
    r = parse((out.stdout or '') + (out.stderr or ''), wall)
    if r is None:
        return {'error': ('exit %d: ' % out.returncode) + ((out.stderr or '')[-300:])}
    return r


def p_postgres(s, wall):
    m = re.search(r'(\d+):(\d+\.\d+) \(m:ss', s)
    if m:
        return {'value': (int(m.group(1)) * 60 + float(m.group(2))) * 1000}
    ms = re.findall(r'([\d.]+)\s*(ms|s)\b', s)
    if not ms:
        return None
    v = float(ms[-1][0]) * (1000 if ms[-1][1] == 's' else 1)
    return {'value': v}


def p_sqlite(s, wall):
    r = {}
    for line in s.splitlines():
        if '\t' in line:
            k, v = line.split('\t')
            r[k] = float(v.split()[0])
    return r or None


def p_us(s, wall):
    m = re.search(r'([\d.]+) us/iter', s)
    return {'value': float(m.group(1))} if m else None


def p_imm(s, wall):
    m = re.search(r'(\d+) turns/s', s)
    return {'value': float(m.group(1))} if m else None


def p_wall(s, wall):
    return {'value': wall}


def tasks():
    t = []
    for rt in BUNS:
        t.append(('http', 'serve', rt, lambda rt=rt: http(rt, 'bun-serve.js', 'http://localhost:3000/')))
    if 'silkjs' in ALL: t.append(('http', 'serve', 'silkjs', lambda: http('silkjs', 'silk-serve.js', 'http://localhost:3000/')))
    for rt in ALL:
        t.append(('http', 'node_http', rt, lambda rt=rt: http(rt, 'node-http.cjs', 'http://localhost:3000/')))
        t.append(('http', 'express_https', rt,
                  lambda rt=rt: http(rt, 'express-tls.mjs', 'https://localhost:3443/', cwd=B + '/express')))
    for rt in BUNS:
        t.append(('ws', 'ws_bun_publish', rt, lambda rt=rt: ws(rt, 'chat-server.bun.js')))
    for rt in ALL:
        t.append(('ws', 'ws_api', rt, lambda rt=rt: ws(rt, 'chat-server.node.mjs')))
    for rt in ALL:
        # silkjs takes ~15 s per 1,000 queries today, so it runs 1,000 (small.mjs) and is scaled x100
        script = 'index.mjs'
        t.append(('pg', 'postgres', rt,
                  lambda rt=rt, script=script: dict(timed(rt, script, B + '/postgres', p_postgres, timeout=150),
                                                    queries=1000 if script == 'small.mjs' else 100000)))
    for rt in ALL:
        t.append(('sqlite', 'sqlite', rt, lambda rt=rt: timed(rt, 'q.mjs', B + '/sqlite', p_sqlite, timeout=300)))
    for rt in ALL:
        t.append(('spawn', 'spawnSync_node_api', rt, lambda rt=rt: timed(rt, 'spawn.mjs', BB, p_us)))
    for rt in BUNS:
        t.append(('spawn', 'spawnSync_bun', rt, lambda rt=rt: timed(rt, 'spawn.bun.mjs', BB, p_us)))
    for rt in ALL:
        t.append(('cat', 'cat_streams', rt, lambda rt=rt: timed(rt, 'cat.cjs', BB, p_wall, timeout=120,
                                                                args=(BIG,), stdout_file='/dev/null')))
    for rt in BUNS:
        t.append(('cat', 'cat_bun_write', rt, lambda rt=rt: timed(rt, B + '/cat/bun.js', BB, p_wall, timeout=120,
                                                                  args=(BIG,), stdout_file='/dev/null')))
    for rt in ALL:
        t.append(('imm', 'setImmediate', rt, lambda rt=rt: timed(rt, 'imm.js', BB, p_imm)))
    return t


def main():
    rounds, out = int(sys.argv[1]), sys.argv[2]
    only = sys.argv[sys.argv.index('--only') + 1].split(',') if '--only' in sys.argv else None
    res = json.load(open(out)) if os.path.exists(out) else []
    ts = [x for x in tasks() if only is None or x[0] in only or x[1] in only]
    for r in range(rounds):
        random.shuffle(ts)
        for g, bench, rt, fn in ts:
            t = time.time()
            try:
                v = fn()
            except Exception as e:
                v = {'error': repr(e)[:300]}
            v.update(bench=bench, runtime=rt, round=r, at=t)
            res.append(v)
            print(json.dumps(v), flush=True)
            json.dump(res, open(out, 'w'), indent=0)


if __name__ == '__main__':
    main()
