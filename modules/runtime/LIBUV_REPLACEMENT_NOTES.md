# Replacing libuv with Silk: working notes

Branch `feature/replace-libuv`, from `main` f3d5ea8. This file tracks the scope, what has moved,
compiler walls, test status and benchmark numbers.

## Status (2026-11-14)

**Nothing has been migrated, built, tested or measured yet.** The first session could not obtain
the Silk toolchain, so only the inventory below (read from the sources, no compiler involved) exists.

Blockers in that session's container:

| Needed | What happened |
| --- | --- |
| Silk 0.1.0-dev.74 toolchain (and Silk's Clang 23) | No `silk` on the machine. Attaching `silk-lang/silk` to the session was denied, and `https://github.com/silk-lang/silk/releases/...` returns 403 from the egress proxy. |
| `/mnt/project-files` (bench harness `bench.py`, `report.py`, `mem.py`, `coldstart.py`, `full2.sh`, `buildrt.sh`, offline dependency archives) | The mount does not exist. |
| `codeload.github.com` (SIMDe archive for WAMR) | 403 from the proxy. `dist.libuv.org` is reachable (200). |

So: no baseline was recorded, no test was run, and no Silk code was written, because none of it
could have been compiled. The standard library of the pinned Silk was also not readable, so the
"what does the stdlib already offer" check is still to do.

## The `uv_*` surface the runtime uses (the scope)

162 distinct `uv_*` functions are declared `extern "C"` in `modules/runtime/src`. By declaring file:

### Loop core — `ext/fs_async.silk`, `ext/uv_fs.silk`, `ext/vm_interrupt.silk`
`uv_loop_init` `uv_loop_close` `uv_loop_size` `uv_loop_configure` `uv_loop_alive` `uv_run`
(`UV_RUN_DEFAULT`, `UV_RUN_NOWAIT`) `uv_stop` `uv_now` `uv_hrtime` `uv_backend_fd`
`uv_backend_timeout` `uv_metrics_idle_time` (`uv_metrics_info` fields are surfaced by `host.silk`)
`uv_walk` `uv_async_init` `uv_async_send`

The libuv loop already runs as a *guest* of `silk.io.reactor`: `fs_async.silk` registers
`uv_backend_fd(loop)` with the reactor (`LIBUV_TOKEN`), bounds the wait by `uv_backend_timeout`,
and calls `uv_run` only when a turn needs it. The replacement removes the guest and puts the
handles on the reactor directly. No `uv_timer_*`, `uv_idle_*`, `uv_prepare_*` or `uv_check_*`
is declared by the runtime: timers, the `setImmediate` queue and loop turns are already Silk
(`runtime/session.silk`). Only uvwasi uses `uv_timer_*` (below).

### Handles (generic) — `ext/uv_net.silk`, `ext/fs_async.silk`, `ext/resource_snapshot.silk`
`uv_handle_size` `uv_handle_get_data` `uv_handle_set_data` `uv_handle_get_type`
`uv_handle_type_name` `uv_close` `uv_ref` `uv_unref` `uv_has_ref` `uv_is_active` `uv_is_closing`
`uv_fileno` `uv_guess_handle`

### Requests and thread pool — `ext/uv_fs.silk`, `ext/fs_async.silk`
`uv_req_size` `uv_req_get_data` `uv_req_set_data` `uv_queue_work` `uv_cancel`
(`UV_THREADPOOL_SIZE` is honoured). `ext/blocking_pool.silk` is an existing Silk pool that
Node-API async work already uses.

### Filesystem requests — `ext/uv_fs.silk` (37, sync when `cb` is null, else on the pool)
`uv_fs_access` `chmod` `chown` `close` `copyfile` `fchmod` `fchown` `fdatasync` `fstat` `fsync`
`ftruncate` `futime` `lchown` `link` `lstat` `lutime` `mkdir` `mkdtemp` `open` `read` `readlink`
`realpath` `rename` `rmdir` `scandir` `scandir_next` `stat` `statfs` `symlink` `unlink` `utime`
`write`, the accessors `uv_fs_get_result` `get_ptr` `get_path` `get_statbuf`, `uv_fs_req_cleanup`.
Layouts: `UvTimespec`, `UvStat`, `UvStatfs`, `UvBuf`, `UvDirent`. Flags: `UV_FS_COPYFILE_EXCL`,
`_FICLONE`, `_FICLONE_FORCE`.

### fs events and fs polling — `ext/fs_async.silk`
`uv_fs_event_init` `start` `stop` (inotify on Linux; kqueue/FSEvents on macOS)
`uv_fs_poll_init` `start` `stop`

### Streams, TCP, pipes, TTY — `ext/uv_net.silk` (used by `ext/net_native.silk`)
`uv_listen` `uv_accept` `uv_read_start` `uv_read_stop` `uv_write` `uv_shutdown`
`uv_stream_set_blocking` (`uv_try_write` is named in a comment only)
`uv_tcp_init` `open` `bind` `connect` `getsockname` `getpeername` `nodelay` `keepalive` `close_reset`
`uv_pipe_init` `open` `bind2` `connect2` `getsockname` `getpeername` `chmod` (`UV_PIPE_NO_TRUNCATE`)
`uv_tty_init` `set_mode` `reset_mode` `get_winsize`
`uv_recv_buffer_size` `uv_send_buffer_size` `uv_ip4_addr` `uv_ip6_addr` `uv_ip4_name` `uv_ip6_name`

### UDP — `ext/uv_net.silk`
`uv_udp_init` `open` `bind` `connect` `getsockname` `getpeername` `recv_start` `recv_stop` `send`
`set_membership` `set_source_membership` `set_multicast_loop` `set_multicast_ttl`
`set_multicast_interface` `set_broadcast` `set_ttl`

### Processes, signals, poll — `ext/process_abi.silk`, `ext/process_native.silk`
`uv_spawn` (`Options`, `Stdio` layouts) `uv_process_get_pid` `uv_process_kill`
`uv_signal_init` `start` `stop` `uv_poll_init_socket` `uv_poll_start` `uv_poll_stop` (child IPC
sockets only). `ext/spawn_sync.silk` already uses `posix_spawn` directly.

### OS queries — `ext/os_abi.silk`, `ext/worker_native.silk`
`uv_available_parallelism` `uv_cpu_info` `uv_free_cpu_info` `uv_interface_addresses`
`uv_free_interface_addresses` `uv_get_total_memory` `uv_get_free_memory`
`uv_get_available_memory` `uv_get_constrained_memory` `uv_resident_set_memory` `uv_getrusage`
`uv_getrusage_thread` `uv_loadavg` `uv_uptime` `uv_exepath` `uv_os_gethostname` `uv_os_getppid`
`uv_os_getpriority` `uv_os_setpriority` `uv_os_homedir` `uv_os_tmpdir` `uv_os_uname`
`uv_os_get_passwd` `uv_os_get_passwd2` `uv_os_free_passwd` `uv_version_string`
`uv_thread_setname`. Layouts: `Cpu`, `CpuTimes`, `Interface`, `Passwd`, `Uname`, `Timeval`, `Usage`.

### Errors — `ext/uv_fs.silk`, `ext/system_errors.silk`
`uv_translate_sys_error`; `system_errors.silk` carries libuv's error names and messages and must
keep them byte for byte (`UV_E*`, `UV_EAI_*`).

### Not used by the runtime
`uv_getaddrinfo` (DNS is already Silk), `uv_getnameinfo` (comment only, `dns_native.silk` does it
itself), `uv_random`, `uv_timer_*`, `uv_idle_*`, `uv_prepare_*`, `uv_check_*`, `uv_dlopen`.

## Other C that touches libuv

| Consumer | Finding | Proposed handling (not yet done) |
| --- | --- | --- |
| `src/ext/inspector_transport.c` | Contains no `uv_*` reference: it uses pthreads and `poll(2)` on its own thread. | Nothing to do for libuv removal. |
| Node-API (`ext/napi_async.silk`) | `napi_get_uv_event_loop` hands addons the runtime's `uv_loop_t*`. Async work and thread-safe functions are already Silk. | An addon that calls `uv_*` on that pointer needs real libuv symbols and layouts. Without libuv the honest options are: return `napi_generic_failure`, or export a C-ABI `uv_*` subset from Silk with libuv 1.x struct layouts. Owner's decision; default plan is the failure code plus a note, since no fixture besides `tests/fixtures/napi/async.c` uses it (to be confirmed by running it). |
| uvwasi (`modules/native/libuv/uvwasi`, e76edba) | Calls 64 libuv functions: the `uv_fs_*` family incl. `opendir`/`readdir`/`closedir`, `uv_loop_init`/`run`/`stop`/`close`, `uv_poll_*`, `uv_timer_init`/`start`, `uv_tcp_init`/`bind`, `uv_listen`/`accept`/`read_start`/`read_stop`/`shutdown`/`try_write`, `uv_mutex_*`, `uv_rwlock_*`, `uv_random`, `uv_gettimeofday`, `uv_hrtime`, `uv_kill`, `uv_os_getpid`, `uv_guess_handle`, `uv_translate_sys_error`, handle/loop data accessors. It embeds `uv_fs_t`, `uv_mutex_t`, `uv_rwlock_t`, `uv_loop_t`, `uv_poll_t`, `uv_timer_t`, `uv_tcp_t` by value. | Keep uvwasi's C unchanged and build it against a small `uv.h` shim header whose functions are `export "C"` Silk. Because uvwasi embeds the structs by value, the shim header fixes their sizes; the Silk side must match. All fs calls are synchronous (null callback); the loop is used only in `poll_oneoff` and the socket helpers. |

## Migration order (plan)

1. Baseline: build `main`, run `silk task compat` and the fixtures, record benchmarks.
2. Handle/request core on `silk.io.reactor` (ref counts, closing queue, `uv_walk` equivalent,
   async wakeup, loop-alive, metrics), with the libuv guest still present for unmoved handles.
3. fs requests and the thread pool (onto `ext/blocking_pool.silk`), then fs events and fs poll.
4. Streams: TCP, pipes, TTY (write queue size and backpressure identical), then UDP.
5. Processes, signals, child IPC poll.
6. OS queries and `uv_translate_sys_error`.
7. uvwasi shim, Node-API decision, remove the guest loop, delete the libuv sources from
   `modules/native/libuv/silk.toml` and the dependency from the runtime's manifest.

macOS arm64: kqueue backend, FSEvents/kqueue fs events, `bsd-ifaddrs`/`darwin.c` equivalents for
the OS queries. Nothing can be run on macOS from a Linux container; it will be written and marked
unverified.

## Compiler walls

None recorded: no Silk code has been compiled yet.

## Test status

Not run (no toolchain).

## Benchmarks

No baseline recorded (no toolchain, no harness).
