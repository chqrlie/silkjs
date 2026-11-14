"""rep.py OCTANE.json MICRO.json [JIT.json] [-a]: the native table (silkjs against V8 jitless and JSC no-JIT).
JIT.json is an octane.py result with v8-jit and jsc-jit columns, shown for context; -a lists every micro test."""
import json,math,os,sys
files=[a for a in sys.argv[1:] if a!='-a']
if len(files)<2: sys.exit(__doc__)
o=json.load(open(files[0])); j=json.load(open(files[2])) if len(files)>2 else o
print("%-16s %8s %10s %10s %7s %7s %9s %9s"%("bench","silkjs","v8-jitless","jsc-nojit","vsV8","vsJSC","v8-jit","jsc-jit"))
rv=[];rj=[]
for k,v in o.items():
    s=max(v.get('silkjs',[0]));a=max(v.get('v8-jitless',[0]));b=max(v.get('jsc-nojit',[0]))
    jj=j.get(k,{})
    if s and a: rv.append(s/a)
    if s and b: rj.append(s/b)
    print("%-16s %8.0f %10.0f %10.0f %7.2f %7.2f %9.0f %9.0f"%(k,s,a,b,s/a if a else 0,s/b if b else 0,max(jj.get('v8-jit',[0])),max(jj.get('jsc-jit',[0]))))
g=lambda x: math.exp(sum(map(math.log,x))/len(x)) if x else float("nan")
print("geomean ratio vs v8 %.2f vs jsc %.2f"%(g(rv),g(rj)))
m=json.load(open(files[1]))
rows=[]
for k,v in m.items():
    if k=='total' or 'silkjs' not in v: continue
    s=min(v['silkjs']);a=min(v.get('v8-jitless',[0]));b=min(v.get('jsc-nojit',[0]))
    rows.append((k,s,a,b,s/a if a else 0,s/b if b else 0))
print("micro: %d tests; geomean silk/v8 %.2f (%d slower), silk/jsc %.2f (%d slower)"%(len(rows),g([r[4] for r in rows if r[4]]),sum(r[4]>1.03 for r in rows),g([r[5] for r in rows if r[5]]),sum(r[5]>1.03 for r in rows)))
for r in sorted(rows,key=lambda r:-max(r[4],r[5])):
    if max(r[4],r[5])>1.03 or '-a' in sys.argv: print("  %-24s %9.1f %9.1f %9.1f  %5.2f %5.2f"%r)
