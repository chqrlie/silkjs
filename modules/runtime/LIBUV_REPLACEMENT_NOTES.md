# Replacing libuv with Silk: working notes

Branch `feature/replace-libuv`, from `main` f3d5ea8. Scope, what moved, compiler walls, test
status and benchmark numbers.

## Status (2026-11-15)

No libuv C is compiled or linked. `modules/native/libuv/silk.toml` has no `[native.archive]` and
compiles only uvwasi, against a header of its own; no Silk file declares an `extern "C" fn uv_*`.

The executable exports **libuv 1.51's whole public C API** (318 `uv_*` functions: every public
function of libuv 1.51 on Unix, and 1.52's `uv_tcp_keepalive_ex` and `uv_udp_open_ex`) for Node-API
addons, written in Silk with libuv's record layouts (`src/ext/uv_api_*.silk`). libuv's own test
suite, linked once against real libuv and once against these exports (`tools/libuv_suite.py`),
passes the same 438 of 442 tests with each on Linux x64.

Verified on Linux x64 and on macOS arm64 (the CI runner): see "macOS arm64" below.

## What replaced what

| libuv part | Now | Where |
| --- | --- | --- |
| Loop, `uv_run`, handle lifetime, ref/unref, close callbacks, `uv_walk`, idle-time metric | Silk, on epoll (Linux) and kqueue (Darwin) | `src/ext/uv_loop.silk` |
| Async wakeups | Silk: an atomic flag and a pipe per loop | `uv_loop.silk` |
| Signals | Silk: one `sigaction` handler, a per-loop pipe, libuv's lock discipline | `uv_loop.silk` |
| Child processes (`uv_spawn`, kill, exit) | Silk: posix_spawn on Darwin as libuv's, fork, child setup and exec elsewhere (and on Darwin for a user or group change), SIGCHLD reaping | `uv_loop.silk`, `src/ext/uv_spawn_darwin.silk` |
| Descriptor poll (child IPC sockets) | Silk | `uv_loop.silk` |
| fs events | Silk: inotify on Linux; on Darwin FSEvents for directories (one CFRunLoop thread and one stream per loop, CoreServices through dlopen) and kqueue vnode for the rest | `uv_loop.silk`, `src/ext/uv_fsevents.silk` |
| fs stat polls (`fs.watchFile`) | Silk: loop timer and `statx` | `uv_loop.silk` |
| Timers | Not used by the runtime (its timers were already Silk); a timer handle exists for addons | `uv_loop.silk` |
| OS queries (`uv_cpu_info`, `uv_interface_addresses`, memory, load, uptime, passwd, uname, rusage, priority, hostname, homedir, tmpdir, exepath, parallelism), `uv_hrtime`, `uv_translate_sys_error`, `uv_thread_setname` | Silk | `src/ext/uv_os.silk` |
| `uv_ip4_addr`, `uv_ip6_addr`, `uv_ip4_name`, `uv_ip6_name` | Silk | `src/ext/uv_inet.silk` |
| What uvwasi calls (63 functions) | Silk, under `silk_uvwasi_*` names | `src/ext/uvwasi_host.silk`, `modules/native/libuv/uvwasi/shim/uv.h` |
| libuv's C API for addons: loops (the default loop included), handles, timers, idle/prepare/check, async, poll, signal, process (pipe and stream stdio), fs event and fs poll handles, `uv_walk`, `uv_print_*` | Silk, libuv's C symbols on the addon's libuv-sized records | `src/ext/uv_api_loop.silk`, `src/ext/uv_api_abi.silk` (record conventions) |
| libuv's threads, locks, once, keys, errors, allocator, OS and address functions, process title, WTF-8 | Silk | `src/ext/uv_api_sys.silk`, `src/ext/uv_errno.silk` |
| libuv's streams, TCP, pipes, TTY and UDP for addons | Silk, on the loop's descriptor handles | `src/ext/uv_api_stream.silk` |
| What an addon may call for fs requests, `uv_queue_work`, `uv_cancel`, `uv_getaddrinfo`/`uv_freeaddrinfo`/`uv_getnameinfo` (with libuv's IDNA) and `uv_random` | Silk, libuv's C symbols and record layouts, on the loop's thread pool | `src/ext/uv_api_fs.silk` |
| TCP, pipes, TTY, UDP, streams, fs requests, the thread pool, DNS, `setImmediate`, loop turns | Already Silk before this branch (`net_native`, `fs_ops`, `blocking_pool`, `dns_native`, `runtime/session`) | unchanged |

The functions keep libuv's names and statuses, so the callers read as before
(`import ext.uv_loop as uvl`). `process.versions.uv` still reports `1.51.0`, the release whose
behavior the code follows.

### The surface, as found on main

162 distinct `uv_*` functions were declared `extern "C"`; 75 were called. The other 87 (all of
`uv_fs_*`, the stream, TCP, pipe, TTY and UDP functions, `uv_queue_work`, `uv_cancel`, the request
accessors) were declarations nothing called, left from before those subsystems moved to the
reactor. They are deleted.

### How the loop sits in the runtime

Unchanged in shape: the handle loop is still a guest of `silk.io.reactor`. `fs_async.silk`
registers the loop's kernel queue descriptor in the reactor, bounds the wait by
`uv_backend_timeout`, and runs the loop without blocking in a turn that needs it. Putting the
handles straight on the reactor (one kernel queue instead of two) is possible later; it needs the
reactor to accept the Darwin vnode filter, or fs events to move elsewhere.

## Other C that touched libuv

| Consumer | Handling |
| --- | --- |
| `src/ext/inspector_transport.c` | Never used libuv (pthreads and `poll(2)`). Nothing changed. |
| uvwasi | Its C is unchanged. It compiles against `uvwasi/shim/uv.h`, a header only, which declares the part of libuv's interface uvwasi uses with layouts of our own (asserted in the header, and again at run time on the Silk side) and renames every function to `silk_uvwasi_*`. `poll_oneoff` gets a private loop over `poll(2)`. |
| Node-API | `napi_get_uv_event_loop` returns the runtime's loop, placed in libuv's `uv_loop_t` size (848 bytes Linux, 1072 Darwin). An addon can call any libuv function on it. Its records (`uv_timer_t`, `uv_tcp_t`, `uv_fs_t`...) have libuv's sizes; their public fields (`data`, `loop`, `type`, `close_cb`, a timer's `timeout`/`repeat`, a process's `pid`, a signal's `signum`, a stream's `write_queue_size`, an fs request's `result`/`ptr`/`path`/`statbuf`...) are kept current, and the private part holds the record's loop handle. `uv_default_loop()` is a separate loop, as it was when the runtime linked libuv (the runtime never ran libuv's default loop). |

## Behavior that differs from libuv, knowingly

- **inotify watches are found by a hash table** where libuv keeps a red-black tree: same results,
  constant-time lookup.
- **`uv_loop_configure` on Darwin arm64** cannot read its variadic argument (compiler wall 9), so
  `UV_LOOP_BLOCK_SIGNAL` takes SIGPROF for granted there.
- **Records outlive their loop handles.** A closed record keeps libuv's closing flags in its own
  `flags` field, so `uv_is_closing` and friends answer after the close callback, as in libuv.
- **Addon API, smaller differences** (each in its module's header): threads start through a Silk
  trampoline (exit value null); no pre-2.21 glibc semaphore fallback; `uv_library_shutdown` frees
  only the `uv_setup_args` copy; no io_uring (libuv 1.51 uses it only when `UV_USE_IO_URING` is
  set); one EMFILE reserve descriptor per process rather than per loop; macOS's select() thread
  for descriptors kqueue refuses (`/dev/tty`) is not done; `uv_tcp_keepalive_ex` and
  `uv_udp_open_ex` follow 1.52's documented interface (1.52's source was not available here).
- **macOS children exit through SIGCHLD.** libuv on macOS learns of a child's exit from a kqueue
  `EVFILT_PROC` filter; the loop here uses its SIGCHLD watcher on both systems. Same callbacks and
  statuses; the runtime installs a SIGCHLD handler on macOS where Node does not.
- **posix_spawn on macOS**: libuv clears `O_NONBLOCK` on the descriptor number a low stdio slot was
  moved to (some unrelated parent descriptor, or none); this clears it on the descriptor given.
- **FSEvents**: the stream's paths array retains its strings (`kCFTypeArrayCallBacks`) and is
  released after the stream is made, where libuv leaks the array, the strings and the C array on
  each reschedule; an unlimited RLIMIT_STACK gives the run-loop thread the default stack where
  libuv aborts; a null stream from FSEventStreamCreate is UV_ENOMEM where libuv asserts; on
  allocation failures a watch is taken out of the stream's list where libuv leaves it in.
- OS queries: where libuv aborts on malformed `/proc` data these carry on; details in the
  `uv_os.silk` header.
- uvwasi shim: socket calls on a descriptor that is not a socket return an error where libuv
  crashed; the wait is `poll(2)`.

## macOS arm64: run on the CI runner

Two workflows run on an Apple Silicon runner (macos-15, Xcode 16.4 SDK): `macos-runtime.yml` builds
the runtime from this branch and, as the baseline, from `main` at f3d5ea8 (libuv linked), and runs
the differential cases and the fixtures with each; `macos-libuv-suite.yml` runs libuv's own suite
against real libuv and against the exports, with `tools/libuv_diagnostic.c` beside it.

Fixtures and differential cases: run of branch commit 29938a7; libuv's suite: run of cf9a9a5 (the
fixes below came between them; a full run of the final commit is queued).

| Check on macOS | main f3d5ea8 | this branch |
| --- | --- | --- |
| Build | passes | passes |
| Differential cases against Node 24 (`tests/compat.mjs`) | 75 passed, 0 failed | 75 passed, 0 failed |
| Fixtures | 77 of 96 | 79 of 98: the same 77, and the two new ones (`fs-watch-darwin-smoke.cjs` with FSEvents, `spawn-smoke.cjs` with posix_spawn, both equal to Node). Every fixture the two share has the same result |
| libuv 1.51's own suite | real libuv: 436 of 441 | Silk exports: 436 of 441; the same five fail with both (`tcp_connect6_link_local`, four UDP multicast tests: the runner's network) |

The 19 fixtures that fail on macOS fail on `main` there too (among them `sea-smoke.py`, the
Node-API smokes, `embed-smoke.py` within the old five-minute limit).

libuv's suite found these differences on macOS that Linux could not show, all fixed:
posix_spawn children got an empty environment when `options.env` was null; `uv_loop_fork` closed
its own new kqueue (a kqueue is not inherited, so the old descriptor's number is free and the new
queue may take it); `ENOBUFS` and `ENOSYS` had Linux's values; `uv_poll_init` set nonblocking
mode with `fcntl`, which a kqueue descriptor refuses (libuv tries `ioctl(FIONBIO)` first); and
`UV_PRIORITIZED` behaved as on Linux where libuv on macOS reports the kernel's refusal of its
`EV_OOBAND` filter as one priority event ahead of the batch. Two more showed only on a busy runner:
`uv_poll` filters registered at once could report in another order than libuv's, which hands its
watcher changes to the kevent call that waits (so a refused change arrives as an `EV_ERROR` event
first); uv_poll handles now keep a per-loop change list that the wait passes to kevent, removals
still happen at once (`poll_oob`). And the loop's clock was `CLOCK_MONOTONIC`, microsecond-grained
on macOS, where libuv's is `mach_continuous_time`, the clock of `uv_hrtime`: a timer could fire
early as `uv_hrtime` measures it (`timer_early_check`).

The runtime's own descriptor watchers do not take the deferred change list: with them on it, the
full run of e55510b timed out in `cluster-transport-smoke.cjs` and `heap-image-smoke.cjs`, which
passed in the run before that change; 271451f registers them at once again.

Not covered by any run: Linux arm64 (type-checked only), the select() thread libuv starts for
descriptors kqueue refuses (`/dev/tty`; not done), and a fork after FSEvents started.

## Compiler walls and tooling issues

None blocked the work, and no C was added. Reported for the compiler's owner:

1. **Internal compiler error on a layout query in `static if`** (silk-lang/silk#3114). Blocks compile-time assertions
   that a Silk `extern "C" struct` matches a C header, so the uvwasi shim checks at run time.
   ```silk
   import silk.mem.layout { Layout }
   extern "C" struct Pair { first: i64, second: i32 }
   fn check() -> () { static if Layout.of<Pair>().bytes != 16 { compileError("Pair layout") } }
   pub fn main() -> i32 { check() return 0 }
   ```
   `internal compiler error[Undetermined]` at the condition. Wanted: `Layout.of<T>()` and an
   `offsetOf<T>("field")` usable in `static if`.
2. **`silk check` does not report SEM0192** (silk-lang/silk#3115) (two declarations of one C symbol with different
   types); only `silk build` does, at codegen. With a 13-minute runtime build that is a late
   failure. Repro: two modules declaring `getpid() -> i32` and `-> i64`.
3. **The check server replays stale diagnostics** (silk-lang/silk#3116, with a minimal repro).
   After editing files, `silk check` answered in 0.04 s with MOD0007 warnings for imports that
   were gone; `silk check --no-server` gave the right answer. Each check server also
   holds about 3.7 GB for this package; three of them plus a build ran the 16 GB container out of
   memory once.
4. **No way to reinterpret `*mut u8` as a pointer to a wider type** (`SEM0215`) even when the
   caller knows the alignment, short of going through `Pointer.assumeAligned`. inotify records are
   read byte by byte for that reason. A documented idiom would do.
5. **No zeroed-value intrinsic for `extern "C"` structs**, so C out-parameters (`getpwuid_r`,
   `getrusage`, `statx`) are either spelled field by field or filled as arrays of words.
6. **No process-wide storage beyond `Intrinsic.processWord()`**, and no way to name a C global.
   Workable: the signal table hangs off a process word, and `environ` is reached with `dlsym`
   (Linux) or `_NSGetEnviron` (Darwin) so that the forked child can replace it before `execvp`.
7. **SEM0212 points at the wrong place** when a `&[u8]` initialized from a literal is later
   assigned a shorter-lived slice (repro in the OS-queries commit message history; the diagnostic
   names neither the variable nor the assignment).
8. `tools/keep_exports.py` cannot keep an export that takes a function pointer; such exports are
   still emitted for this package (the addon fixture links them), so this is a note, not a wall.
9. **No variadic `export "C"` functions.** libuv's `int uv_loop_configure(uv_loop_t*, uv_loop_option, ...)`
   cannot be defined with its C signature; `ext.uv_api_loop` exports it with a fixed third `int`.
   On Linux x86-64 and GNU arm64 a variadic caller passes that argument in the same register, so
   it behaves the same. On Darwin arm64 a variadic argument goes on the stack, so
   `UV_LOOP_BLOCK_SIGNAL` there takes `SIGPROF` (the only signal libuv accepts) without reading it,
   and an addon passing another signal gets success where libuv returns `UV_EINVAL`. Wanted:
   `export "C" fn f(a: A, ...)` with a `VaList` reader, as calls to variadic externs already work.

## Test status (Linux x64, release build of this branch)

| Suite | Result |
| --- | --- |
| `silk task compat` cases against Node 24.22 (`tests/compat.mjs`) | 75 passed, 0 failed (main: 75, 0) |
| Fixtures in `tests/` (`*-smoke.cjs`, `*-smoke.mjs`, the wasm and WASI fixtures, the Python ones) | 83 of 96 pass: the 81 that pass on `main` and the two new ones. The same 13 fail on the `main` binary built here, with the same last line of output, so they predate this branch or need a setup this container lacks: crypto-public, error-stack, gc-profiler, inspector-transport, inspector-websocket, iterable-streams, module-compile-cache, permission-matrix, process-report-fatal, util-abort-transfer, util-call-sites, vm-diagnostic-offsets, wasm-memory-range |
| Not run | `embed-smoke.py`, `sea-smoke.py` (each builds another executable), Node's own suite under `tests/node-suite` |
| Fixtures added on this branch: `tests/spawn-smoke.cjs` (9 scenarios), `tests/fs-watch-darwin-smoke.cjs` (7 directory-watch scenarios), each compared with Node 24 | pass (Linux: fork and exec, inotify) |
| **libuv 1.51's own test suite** (`tools/libuv_suite.py`: libuv's `test/*.c` linked with libuv built from source, and with `ext.uv_api_*`), run as `nobody` | libuv 438 of 442, Silk 438 of 442; the same four fail with both, for this container: `fs_copyfile`, `tcp_close_while_connecting` and `tcp_connect_timeout` (no outside network), `thread_priority` (unprivileged). The four `test-idna.c` tests exercise libuv's internal IDNA code, not its interface, and are left out. It found five differences on the way (a timer overflow, closed records, poll `EEXIST`, signal counts after `uv_loop_fork`, the thread pool after fork), all fixed |
| Standalone harness of `uv_loop.silk` (async, signal, spawn, kill, missing program, fs event, stat poll, close order, pool, phases, timers, walk) | passes; outside the repository |
| OS queries against real libuv 1.51 (same program, both libraries): real machine, 23 substituted `/proc` and cgroup scenarios, synthetic `getifaddrs`, 89,118 address texts | identical output |
| uvwasi driver against real libuv 1.51 and against the shim (about 140 lines: fs, `poll_oneoff`, sockets) | identical but for four expected lines |
| libuv 1.51's own fs, thread-pool, cancel, getaddrinfo, getnameinfo, IDNA and random tests (80 on Linux), linked against `uv_api_fs.silk` and against real libuv, run as `nobody`; plus a differential program (about 170 lines of results, fields and cleanup) | all 80 pass on both; differential output identical. Harness outside the repository (`/home/user/dl/uvapi-tests/fs`) |

## Benchmarks

Container: 4 vCPU Linux x64. Release builds, Silk dev.74, `SILKJS_PREPARSE_THREADS=0`, harness
`bun-bench-2026-11-14-nosmol` (server on CPU 0, clients on CPUs 2 and 3).

### Baseline, main f3d5ea8 against Bun 1.4.4 JIT off (6 rounds, medians)

| Benchmark | silkjs main | Bun JIT off |
| --- | --- | --- |
| serve() hello, req/s | 135,318 | 124,502 |
| node:http hello, req/s | 9,172 | 10,837 |
| Express over HTTPS, req/s | 3,105 | 5,942 |
| WebSocket chat (ws API), msg/s | 1,476,452 | 225,034 |
| Postgres 100k queries, ms | 5,983 | 7,125 |
| SQLite SELECT Order, ms | 15.58 | 17.57 |
| SQLite SELECT Product, ms | 0.032 | 0.037 |
| SQLite SELECT OrderDetail, ms | 183.5 | 259.2 |
| SQLite db.exec 8000 statements, ms | 5.24 | 5.59 |
| spawnSync echo, us | 1,294 | 1,476 |
| cat 1 GB with fs streams, ms | 681 | 1,328 |
| setImmediate, turns/s | 1,540,483 | 1,269,136 |

### Before and after: main against this branch, interleaved

Same harness, silkjs only. Builds: `main` as above; the branch at its final state (the whole libuv
API exported, link-order hints regenerated). Wall-clock numbers in this container swing 5% to 10%
between rounds, so each comparison was run twice, the second time alternating which binary runs
first in a round, and the work itself was counted with callgrind.

Peak RSS, MB (median of six or eight interleaved rounds; main / branch):

| Benchmark | main | branch |
| --- | --- | --- |
| Cold start, empty script | 17.1 | 16.0 |
| Cold start, `net` | 26.7 | 25.5 |
| serve() hello under load | 30.4 | 29.1 |
| node:http hello under load | 31.9 | 31.1 |
| Express over HTTPS under load | 55.7 | 54.4 |
| WebSocket chat | 77.6 | 77.6 |
| setImmediate / spawnSync / cat / Postgres / SQLite | 22.1 / 25.0 / 26.9 / 48.7 / 174.4 | the same within 0.2 MB |

Instructions executed (callgrind, same work; main / branch): 200,000 setImmediate turns
600.0 M / 584.2 M (-2.6%); a node:http server answering 5,000 keep-alive requests 1,337 M /
1,306 M (-2.3%); serve() answering 5,000 requests 171.7 M / 166.2 M (-3.2%).

Throughput and server CPU, alternating order (medians; main / branch): Postgres 100k queries
8,056 / 8,039 ms; setImmediate 1.59 M / 1.58 M turns/s; SQLite SELECT Order 21.5 / 20.2 ms,
OrderDetail 218.9 / 214.3 ms, `db.exec` 8000 statements 5.69 / 5.06 ms; serve() 120.6 k / 116.1 k
req/s (ranges 115.7..127.3 k / 110.0..131.7 k); node:http 9,207 / 8,611 req/s (7,987..9,625 /
7,780..9,368); Express over HTTPS 2,727 / 2,770 req/s; spawnSync 1,489 / 1,470 us; cat 1 GB
928 / 799 ms. In the first (fixed-order) run serve() read +2.1% and node:http -1.1%; the two
runs disagree in sign, the ranges overlap, and the instruction counts are lower on the branch, so
these are read as noise.

Cold start, wall ms (30 interleaved runs, median; main / branch): empty 6.6 / 7.5, hello 6.5 / 6.8,
cjs 7.2 / 6.9, esm 12.2 / 9.9, local 8.8 / 8.2, fsread 7.8 / 7.6, timer 8.1 / 7.7, net 15.1 / 15.2;
page faults 1 to 2% fewer on the branch in every case.

Reading it:

- **A memory regression was found and fixed:** the first full build of this branch started at
  35.3 MB resident (main 22.8 MB), every server 13 to 17 MB higher. Silk names functions by hash,
  and nearly all names had changed, so `tools/startup-order.txt` matched 38 of its 896 functions
  and the start's code was spread over the text again. Regenerated (`tools/startup_order.py`, with
  callgrind profiles of node:http, serve() and Express servers under `wrk -c8`): 22.0 MB. **The
  lists must be regenerated whenever the function names move**; a build with a stale list is
  correct but larger.
- An earlier fixed-order run showed Postgres +5.6% and setImmediate -5.6%; with the order
  alternated they are -0.2% and -0.6%. The binary that ran second in each round was penalized.
- The engine benchmarks (section 4 of the guide) were not run: no engine source changed.

## Next

- A passing run of the macOS workflow, FSEvents and posix_spawn included.
- Rename `modules/native/libuv` (it holds only uvwasi now).
- Put the standalone loop harness in the repository as a test package.
