#!/usr/bin/env python3
"""Peak RSS (ru_maxrss via wait4) of the non-server benchmarks, interleaved rounds. usage: mem.py ROUNDS OUT.json"""
import json, os, random, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench
CASES = [('postgres', 'index.mjs', bench.B + '/postgres', ()), ('sqlite', 'q.mjs', bench.B + '/sqlite', ()),
         ('cat_streams', 'cat.cjs', bench.BB, (bench.BIG,)), ('spawnSync_node_api', 'spawn.mjs', bench.BB, ()),
         ('setImmediate', 'imm.js', bench.BB, ())]
res = []
for r in range(int(sys.argv[1])):
    ts = [(c, rt) for c in CASES for rt in bench.ALL]
    random.shuffle(ts)
    for (name, script, cwd, args), rt in ts:
        argv, env = bench.cmd(rt, script, *args)
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _, st, ru = os.wait4(p.pid, 0)
        v = dict(bench=name, runtime=rt, round=r, rss_mb=ru.ru_maxrss / 1024, ok=st == 0)
        print(json.dumps(v), flush=True); res.append(v)
json.dump(res, open(sys.argv[2], 'w'), indent=0)
