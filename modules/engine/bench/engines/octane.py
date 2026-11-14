#!/usr/bin/env python3
"""Full Octane 2 per benchmark, one process each, interleaved rounds; best score per engine.
usage: octane.py ROUNDS OUT.json name=cmd... [--only a b]     (silk task bench-octane runs this)
Writes the combined scripts to scripts/ in the current directory."""
import json, os, re, shlex, subprocess, sys
# A checkout of github.com/chromium/octane; `silk task bench-setup` clones it into the work directory.
O = os.environ.get("OCTANE_DIR") or sys.exit("octane.py: set OCTANE_DIR to a checkout of github.com/chromium/octane")
GROUPS = [("richards", ["richards"]), ("deltablue", ["deltablue"]), ("crypto", ["crypto"]), ("raytrace", ["raytrace"]),
          ("earley-boyer", ["earley-boyer"]), ("regexp", ["regexp"]), ("splay", ["splay"]), ("navier-stokes", ["navier-stokes"]),
          ("pdfjs", ["pdfjs"]), ("mandreel", ["mandreel"]), ("gbemu", ["gbemu-part1", "gbemu-part2"]), ("code-load", ["code-load"]),
          ("box2d", ["box2d"]), ("zlib", ["zlib", "zlib-data"]), ("typescript", ["typescript", "typescript-input", "typescript-compiler"])]
RUN = """var __print = (typeof print === 'function') ? print : console.log.bind(console);
BenchmarkSuite.RunSuites({
  NotifyResult: function(name, result) { __print(name + ': ' + result); },
  NotifyError: function(name, error) { __print(name + ': ERROR ' + error); },
  NotifyScore: function(score) { }
});
"""
def main():
    a = sys.argv[1:]
    only = None
    if "--only" in a:
        i = a.index("--only"); only = a[i+1:]; a = a[:i]
    rounds = int(a[0]); out = a[1]
    engines = [(x.split("=", 1)[0], shlex.split(x.split("=", 1)[1])) for x in a[2:]]
    os.makedirs("scripts", exist_ok=True)
    res = {}
    for g, files in GROUPS:
        if only and g not in only: continue
        p = "scripts/%s.js" % g
        with open(p, "w") as f:
            f.write("if (typeof read === 'undefined') var read = function(){ throw new Error('no read'); };\n")
            for n in ["base"] + files: f.write(open("%s/%s.js" % (O, n), encoding="utf-8", errors="surrogateescape").read() + "\n")
            f.write(RUN)
        for r in range(rounds):
            for name, cmd in engines:
                try:
                    o = subprocess.run(cmd + [p], capture_output=True, text=True, timeout=600)
                    txt = o.stdout
                except subprocess.TimeoutExpired:
                    txt = ""
                found = re.findall(r"^(\w+): (\S+)$", txt, re.M)
                if not found: print("FAIL", g, name, (txt + o.stderr)[-300:], flush=True)
                for k, v in found:
                    try: v = float(v)
                    except ValueError: print("ERR", k, name, v); continue
                    res.setdefault(k, {}).setdefault(name, []).append(v)
        for k in res:
            pass
        print(g, {k: {n: max(v) for n, v in res[k].items()} for k in res if k.lower().startswith(g.replace("-", "")[:5])}, flush=True)
        json.dump(res, open(out, "w"), indent=1)
if __name__ == "__main__":
    main()
