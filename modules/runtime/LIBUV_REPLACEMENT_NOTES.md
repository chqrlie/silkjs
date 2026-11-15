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

Verified on Linux x64. macOS arm64 is written, type-checks and passes code generation
(`silk build --target aarch64-apple-darwin`); the macOS runner (`.github/workflows/macos-runtime.yml`)
is the first place it runs: see "macOS arm64" below.

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

## macOS arm64: written, first run on the CI runner

The Darwin branches type-check and pass code generation here (the link needs the SDK); the
`macOS runtime` workflow builds the runtime on an Apple Silicon runner and runs the differential
cases, the fixtures and libuv's own suite against the exports. Until a run of it passes, treat
everything below as unverified:

1. **FSEvents and posix_spawn are written, not run.** `src/ext/uv_fsevents.silk` (directory
   watches, recursive watches) and `src/ext/uv_spawn_darwin.silk` type-check and link for
   aarch64-apple-darwin against the macOS 15.5 SDK; their constants and layouts come from its
   headers. `tests/fs-watch-darwin-smoke.cjs` and `tests/spawn-smoke.cjs` compare them with Node
   on the macOS runner.
2. Every Darwin constant and layout is from headers and libuv's source, unverified: `sigaction`,
   signal numbers, `kevent`, `stat`, `sockaddr_dl`, sysctl MIBs, Mach `host_statistics` and
   `task_info`, `passwd`, `utsname`.
3. The kqueue backend of the loop, the signal handler and the fork path have never executed there.

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

Same harness, silkjs only: six rounds, each running the `main` binary and then this branch's
(final build, with `tools/startup-order.txt` and `tools/server-order.txt` regenerated). Medians;
the last column is the range of the six values, main then branch.

| Benchmark | main | branch | change | ranges |
| --- | --- | --- | --- | --- |
| serve() hello, req/s | 135,634 | 132,566 | -2.3% | 133,272..140,381 / 114,214..138,784 |
| serve(): user us/request | 2.54 | 2.81 | +10.8% | 2.40..2.77 / 2.44..3.54 |
| serve(): sys us/request | 4.73 | 4.76 | +0.6% | |
| serve(): peak RSS, MB | 30.9 | 30.7 | -0.8% | |
| node:http hello, req/s | 9,298 | 9,282 | -0.2% | 8,710..9,550 / 9,148..9,673 |
| node:http: user us/request | 93.0 | 93.1 | 0.0% | |
| node:http: sys us/request | 13.6 | 13.0 | -4.5% | |
| node:http: peak RSS, MB | 32.3 | 32.3 | -0.2% | |
| Express over HTTPS, req/s | 3,269 | 3,220 | -1.5% | 3,020..3,574 / 2,967..3,376 |
| Express: user us/request | 283.9 | 289.9 | +2.1% | 264..308 / 277..314 |
| Express: sys us/request | 20.9 | 19.0 | -8.9% | |
| Express: peak RSS, MB | 56.6 | 57.0 | +0.6% | |
| WebSocket chat (ws API), msg/s | 1,540,840 | 1,542,328 | +0.1% | |
| WebSocket: peak RSS, MB | 78.7 | 79.0 | +0.4% | |
| Postgres 100k queries, ms | 6,501 | 6,402 | -1.5% | |
| SQLite SELECT Order, ms | 14.57 | 15.23 | +4.6% | 13.87..17.20 / 14.09..16.48 |
| SQLite SELECT Product, ms | 0.030 | 0.032 | +5.0% | 0.029..0.031 / 0.030..0.035 |
| SQLite SELECT OrderDetail, ms | 185.0 | 185.6 | +0.3% | |
| SQLite db.exec 8000 statements, ms | 5.21 | 5.61 | +7.7% | 5.04..5.83 / 5.18..5.98 |
| spawnSync echo, us | 1,302 | 1,215 | -6.7% | 1,194..1,357 / 1,149..1,269 |
| cat 1 GB with fs streams, ms | 711 | 709 | -0.4% | 636..856 / 636..740 |
| setImmediate, turns/s | 1,638,273 | 1,613,451 | -1.5% | |

Peak RSS of the non-server benchmarks (MB, median of 3; main / branch): cat 28.5 / 28.4,
Postgres 50.9 / 52.5, setImmediate 23.4 / 23.0, spawnSync 26.3 / 26.2, SQLite 177.3 / 178.1.

Cold start (30 interleaved runs, wall ms median; main / branch): empty 5.2 / 4.8, hello 5.6 / 5.2,
cjs 5.9 / 5.2, esm 8.1 / 7.4, local 6.2 / 5.5, fsread 6.9 / 5.8, timer 6.4 / 5.9, net 10.9 / 10.2.
Peak RSS of a start: empty 17.4 / 16.3 MB, net 27.1 / 27.2 MB.

Reading it:

- Throughput, server CPU and memory are within the run-to-run range on every runtime benchmark,
  with these to watch:
- **SQLite `db.exec` reads 7.7% slower (and 7.6% in the earlier run), and it is code layout, not
  work.** Under callgrind, 20 runs of the 8000-statement `exec` execute 961,680,770 instructions on
  the `main` binary and 961,682,444 on this branch's, with the same count in every SQLite function.
  In isolation (400 runs, pinned) the median is 10.75 ms against 11.15 ms. Nothing on that path
  changed; SQLite's code sits at different addresses because other objects left the link. The
  guide's own caution applies (wall time swings 4% to 7% between builds at equal instruction
  counts). The two short SQLite reads lean the same way for the same reason.
- serve() user time per request reads +10.8% here and -0.4% in the earlier interleaved run of the
  same code; one branch round was an outlier (114k req/s). Treated as noise, not confirmed.
- **The Postgres peak RSS difference (50.9 / 52.5 MB above) was a measurement artifact.** The
  memory pass ran all of main's rounds and then all of the branch's, not interleaved, and the
  database had grown in between. Interleaved, with the harness's own method (`wait4` `ru_maxrss`),
  three runs each give 48.9 to 49.0 MB on main and 48.6 to 48.9 MB on the branch.
- **A layout regression was found and fixed on the way:** the first build of this branch had every
  peak RSS about 1.2 to 1.9 MB higher, all of it resident text and read-only data. The functions a
  start runs had changed, so the link-order hints no longer covered them. Regenerating the hints
  from this branch's binary (merged with the committed lists, which also hold profiles this
  container cannot retake) brought a start back from 27.7 MB to 26.6 MB (main: 26.5 MB).
- The engine benchmarks (section 4 of the guide) were not run: no engine source changed.

## Next

- A passing run of the macOS workflow, FSEvents and posix_spawn included.
- Rename `modules/native/libuv` (it holds only uvwasi now).
- Put the standalone loop harness in the repository as a test package.
