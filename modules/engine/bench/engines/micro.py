#!/usr/bin/env python3
"""usage: micro.py OUT.json ROUNDS name=cmd...   (cmd gets the micro script appended; best ns per test)"""
import json, re, shlex, subprocess, sys
LINE = re.compile(r"^\s*(\w+)\s+(\d+)\s+([\d.]+)")
out, rounds = sys.argv[1], int(sys.argv[2])
engines = [(x.split("=", 1)[0], shlex.split(x.split("=", 1)[1])) for x in sys.argv[3:]]
res = {}
for r in range(rounds):
    for name, cmd in engines:
        o = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        n = 0
        for l in o.stdout.splitlines():
            m = LINE.match(l)
            if m and m.group(1) != "TEST":
                res.setdefault(m.group(1), {}).setdefault(name, []).append(float(m.group(3))); n += 1
        print(r, name, n, "tests", o.stderr[-200:], flush=True)
        json.dump(res, open(out, "w"), indent=1)
