# fx.py ROUNDS name1,name2 BIN...: runs fx-<name>.js (in the current directory) under each binary, interleaved, prints min user time.
# Make fx-<name>.js by concatenating octane base.js, the benchmark files and:
#   for (var s=0;s<BenchmarkSuite.suites.length;s++){var b=BenchmarkSuite.suites[s].benchmarks[0]; BenchmarkSuite.ResetRNG(); if (b.Setup) b.Setup(); for (var k=0;k<N;k++) b.run(); }
import subprocess,resource,sys
rounds=int(sys.argv[1]); names=sys.argv[2].split(','); bins=sys.argv[3:]
for n in names:
    allt={b:[] for b in bins}
    for r in range(rounds):
        for b in bins:
            r0=resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
            p=subprocess.run([b,'fx-%s.js'%n],capture_output=True,text=True)
            if p.returncode: print('FAIL',b,n,p.stderr[-200:])
            allt[b].append(resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime-r0)
    m0=min(allt[bins[0]])
    print('%-14s'%n+'  '.join('%s %.3f (%.3fx)'%(b.split('/')[-1],min(allt[b]),min(allt[b])/m0) for b in bins),flush=True)
