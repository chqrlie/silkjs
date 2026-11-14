#!/usr/bin/env python3
"""rep-wasm.py OCTANE.json [MICRO.json]: tables with the WebAssembly columns.
Octane is best score (higher is better); micros are best ns/op (lower is better).
Ratios: wasm/qjs-wasm compares the two wasm builds; wasm/native is silkjs.wasm against native silkjs."""
import json, math, sys
ORDER = ["silkjs", "v8-jitless", "jsc-nojit", "qjs", "silkjs-wasm", "qjs-wasm", "v8-jit", "jsc-jit"]
g = lambda x: math.exp(sum(map(math.log, x)) / len(x)) if x else float("nan")
def table(data, best, better):
    names = [n for n in ORDER if any(n in v for v in data.values())]
    print("%-24s" % "bench" + "".join("%12s" % n for n in names) + "%14s%13s" % ("wasm/qjs-wasm", "wasm/native"))
    r1, r2 = [], []
    for k, v in data.items():
        if k == "total": continue
        val = {n: best(v[n]) for n in names if v.get(n)}
        a, b, s = val.get("silkjs-wasm"), val.get("qjs-wasm"), val.get("silkjs")
        x = better(a, b) if a and b else None
        y = better(a, s) if a and s else None
        if x: r1.append(x)
        if y: r2.append(y)
        fmt = "%12.0f" if max(val.values()) >= 100 else "%12.1f"
        print("%-24s" % k + "".join(fmt % val[n] if n in val else "%12s" % "-" for n in names)
              + ("%14.2f" % x if x else "%14s" % "-") + ("%13.2f" % y if y else "%13s" % "-"))
    print("geomean: silkjs-wasm is %.2fx qjs-wasm and %.2fx native silkjs (above 1 means silkjs-wasm is faster)" % (g(r1), g(r2)))
o = json.load(open(sys.argv[1]))
print("Octane (score, higher is better)")
table(o, max, lambda a, b: a / b)
if len(sys.argv) > 2:
    print("\nMicro (ns/op, lower is better)")
    table(json.load(open(sys.argv[2])), min, lambda a, b: b / a)
