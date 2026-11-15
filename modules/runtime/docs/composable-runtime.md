# Composable runtime for embedders: design

Nov 14, 2026 · @Julia

## Summary

The runtime should become a small core plus a fixed list of capability services (files, network, crypto, Wasm, SQLite and so on). Each is a Silk `service` the embedder must provide at start. For a capability it does not want, the embedder provides the empty implementation the runtime ships (`Files.empty`, `Wasm.empty`), and that capability's modules, native code and C library are not in the binary. Today it is one package: `libs.core.host` imports all 41 native extension modules, one `Services` struct holds a state pointer for each, and `silk.toml` compiles WAMR, llhttp, OpenSSL, nghttp2, SQLite, zlib, zstd, brotli and libuv into every build, embedded or not.

The design rests on four rules.

1. **Every capability is a required service.** `embed.start` lists each one in its requirements, and the embedder answers each with `Effect.provideMut`: the real implementation the runtime ships, a type of its own, or the shipped empty one. There is no extension list to assemble.
2. **The implementation's type decides what is linked, its value decides what is granted.** Whether a capability is present is a constant of the implementation type, so an empty implementation removes the modules and the native code at build time, and its C library is never a dependency. A real implementation's value (which directory, which hosts, which environment) decides what JavaScript may touch.
3. **Nothing is optional or defaulted.** A new capability adds a service to the list, so every embedder fails to compile until it decides, even when the decision is the empty implementation. This follows the standing rule of no compat defaults.
4. **An absent capability is absent, not stubbed.** With `Files.empty`, `require('node:fs')` fails with the error Node itself uses for a module its build lacks, `process.features` and `builtinModules` say so, and with `Wasm.empty`, `typeof WebAssembly` is `'undefined'`. An embedder who wants a module that exists but is empty or denied provides a memory or denying implementation instead.

Build-time selection follows the package graph: each real implementation that needs a C library lives in its own package, so the library is compiled only when that package is a dependency. Silk package parameters (`pub param`) are kept for scalar knobs only. The heap image and built-in bytecode become a build step any embedder can run for its own choice of implementations, so an embedded runtime can start from an image as the executable does.

The migration is eight steps, each landing on main with the executable unchanged. The first proof arrives at step 1: the embedding smoke test built with `Wasm.empty`, with a check that no WAMR symbol is in the binary. Step 1 also measures the one thing this shape depends on, which is carrying the services through the engine as Effect requirements. Sizes per capability are not measured yet, and step 0 produces that table before boundaries are fixed.

## Current state

Every capability is wired into one place and linked unconditionally. Read at lib-silkjs main `197416d`.

**How a native call travels.** `worker.silk` `configure` defines the native functions on a `__silkNative` object, each a `NativeFunctionId.Host` with a hand-allocated number (fs 2000 to 2007, net 2400 to 2405, tls 2500, workers 6000, sqlite 7000, wasm 9000, wasi 10000, napi 14000 and so on). `bootstrap.js` spreads that object, plus the engine's QuickJS-style `silk:host/std` and `silk:host/os` modules, into `host`, which the built-in JavaScript uses. Calls come back through `Services.nativeGeneric` in `libs/core/host.silk`, an if-chain over id ranges that forwards to each extension's `dispatch`. The same file answers the engine's other hooks (exotic objects for vm and web storage, tracers and finalizers for Wasm, external buffer release, interrupts) by the same ids.

**What is fixed in the type.** `host.silk` imports 41 `ext.*` modules. `Services` has one nullable state pointer per extension (`io`, `crypto`, `network`, `tls`, `http2`, `sqlite`, `napi`, `wasm`, `wasi`, `httpParser`, `tracing`, `inspector`, `sea`, `compression`, `processes`, `workers`). The loop's poll phase calls `fs_async`, `crypto_native`, `net_native`, `compression_native`, `process_native`, `napi_native` and `wasm_native` by name. Startup and teardown in `worker.silk` open and close each by name.

**What is linked.** The README says it plainly: "Every extension is still linked into an embedder, and the heap image is the executable's alone, so an embedded runtime bootstraps cold."

| Capability | Native side today | Behind it | Hard-wired where |
| --- | --- | --- | --- |
| Event loop, timers | `fs_async.silk` owns a `silk.io.reactor` Reactor with libuv as a guest, plus a blocking pool; `immediates.silk` | libuv 1.51 (still used by 11 files) | Created first; process, napi and inspector take its pointer |
| File system | `fs_native`, `fs_async`, `fs_ops`, `uv_fs`, `fs_descriptors` | libuv fs requests, libc | Loader, compile cache, reports, heap snapshots, SEA, DNS config and SQLite each reach files their own way |
| Network, DNS | `net_native` (201 KB), `dns_native`, `uv_net` | `silk.net.socket_native`, `silk.net.dns`, libuv | `net` and `http` are preloaded into the heap image |
| HTTP | `http_parser_native`, `http2_native` | vendored llhttp plus a C bridge, nghttp2 1.66 | llhttp is in the runtime's own `[native] sources` |
| TLS, crypto | `tls_native`, `crypto_native` (135 KB), `openssl*` | OpenSSL 3.5.4 | Only these two import it, but it is always linked |
| Compression | `compression_native` | zlib 1.3.1, zstd 1.5.7, brotli | `os_native` imports its ABI too |
| WebAssembly, WASI | `wasm_native` (105 KB), `wasm_ownership`, `wasi_native` | WAMR interpreter with patches, SIMDe | About 40 WAMR C files listed in the runtime manifest; `host.silk` names its class ids 9096 to 9098 |
| SQLite, web storage | `sqlite_native`, `webstorage_native` | `db-sqlite` package | `localStorage` requires `internal/sqlite/database` |
| Workers | `worker_native` (92 KB), transport, locks | engine `js_os_workers`, threads | `workerNative` is the most used bridge (53 call sites), also for exit and embedding |
| Child processes | `process_native`, `spawn_sync` | libuv process handles, `silk.system.subprocess` | Shares the io loop |
| Inspector, profiling | `inspector_*`, `profile_*`, `coverage_native`, `trace_native` | `inspector_transport.c` (own sockets) | State is created at every start |
| Node-API | `napi_*` (4 files, 225 KB) | `dlopen` | Opened at every start |
| Process, OS | `process_native`, `os_native`, engine std and os modules | libc, libuv | `process.env` reads `getenv` live; cwd, pid, kill and exit go straight to libc |

Five facts shape the design.

- **Raw libc is ambient.** `src/` has 1,479 `extern "C" fn` declarations across 55 files, and `rawOS.open`, `rawStd.getenv`, `rawOS.kill` and friends are reachable from built-in JavaScript. A provider boundary means nothing until these are behind it.
- **JavaScript is one bundle.** `tools/embed.py` embeds all 561 built-in files (11.4 MB of source) with no notion of owner. `publicBuiltinNames` is every non-internal factory. Three vendored files are a large share: amaro 3.8 MB, undici 0.77 MB, acorn 0.25 MB.
- **Capabilities depend on each other in ways the layout hides.** TypeScript stripping (amaro) runs on WebAssembly. `localStorage` needs SQLite. `fetch` needs net, TLS and compression. The loader reads modules through fs natives.
- **There is a per-id permission hook already.** `permission_gate.silk` calls a JavaScript policy before guarded ids (fs, process, workers, SQLite, WASI, napi, inspector). It implements Node's `--permission` and stays, layered above capability absence.
- **An embedding API exists and is the right base.** `runtime.embed` gives `Host.drive`, `Runtime.turn`, `waker`, `evaluateModule` and an `Embedder` slot for the host's own native functions (ids from 1,000,000) through a C function pointer.

The Silk standard library already has the provider pattern this design needs: `FileSystem`, `ChildProcess`, `Random`, `SystemClock`, `MonotonicClock` and `Http` are effect requirements (`? &mut FileSystem`) satisfied with `Effect.provideMut`, and `OsFileSystem.make(root)` is rooted at a directory. The package manager in `modules/pm` is written that way. The runtime's extensions mostly bypass it.

## Prior art

The runtimes that embed well separate three things this runtime merges: the unit of surface (an extension), the resource behind it (a provider or binding), and the per-call policy (permissions). Rows for QuickJS and Bun are from memory, not re-read for this doc.

| Runtime | Unit of composition | Chosen when | Without the capability | What to take |
| --- | --- | --- | --- | --- |
| [Deno `deno_core`](https://docs.rs/deno_core/latest/deno_core/macro.extension.html) | `extension!`: `ops`, `esm`, `lazy_loaded_esm`, `deps`, `state`, `options`; generic `parameters` carry the permission trait | Build time: the embedder passes a list of extensions to the runtime, and the snapshot is built from the same list | The op and its JS do not exist. Linked ops check a permissions object per call | Extensions bundle native ops, JS and state, declare dependencies, and are generic over their policy type. The snapshot is a function of the extension list |
| [workerd](https://www.mintlify.com/cloudflare/workerd/concepts/capabilities) | Bindings in config: a named grant that appears in `env` | Run time, per worker | "Cannot access any resource not present in `env`". No outbound fetch without `globalOutbound` | No ambient authority: a resource is a value someone handed over, not a global name. One binary serves tenants with different grants |
| [workerd `node:fs`](https://developers.cloudflare.com/workers/runtime-apis/nodejs/fs/) | A virtual file system behind the real module | Compatibility flag | `/bundle` read-only modules, `/tmp` in memory per request, `/dev` devices | "Bring your own file system" works for unmodified packages when the module is real and the storage is virtual |
| [wasmtime WASI](https://docs.wasmtime.dev/api/wasmtime_wasi/struct.WasiCtxBuilder.html) | `WasiCtxBuilder` plus host traits per interface | Run time, per instance | Empty by default: stdin closed, no env, no args, "no preopened directories", name lookup denied | Start from nothing and add. Clocks and random are replaceable providers too. A directory grant is a root handle with permissions |
| QuickJS | `JS_NewContextRaw` plus `JS_AddIntrinsic*`; `std` and `os` live in `quickjs-libc.c`, outside the engine; the embedder sets the module loader | Build and run time | The global or module is not there | The engine has no OS. Our engine already matches this (it builds to wasm32); the runtime does not |
| [Javy](https://docs.rs/javy-plugin-api/4.1.0/javy_plugin_api/struct.Config.html) | A `Config` of intrinsics (`event_loop`, `text_encoding`, `javy_stream_io`, output streams) fixed when the plugin is built | Build time of the plugin | Intrinsic absent | The capability set is frozen into the artifact, and output streams are a provider |
| [Node](https://nodejs.org/download/nightly/v27.0.0-nightly20261018f88a1d25e9/docs/api/embedding.md) | Configure flags (`--without-ssl`, `--without-intl`, `--without-inspector`) and the C++ embedder API | Build time for the whole binary | `require('crypto')` throws `ERR_NO_CRYPTO`; inspector calls throw `ERR_INSPECTOR_NOT_AVAILABLE`; `process.features` reports it | Node already defines how a build without a capability behaves, with error codes its own tests and packages check. The embedder API cannot pick modules, and the docs warn it breaks on majors |
| Bun | None public. One binary, `bun build --compile` for apps, plugins for virtual modules | Not offered | Not offered | A gap this runtime can fill |

Three lessons carry into the proposal.

- Deno's lesson fits Silk: what is composed must be known at build time, and the snapshot follows it. A Silk service provided at start is specialized like a Rust generic, so the choice costs nothing at dispatch and a constant in the implementation can remove what is not wanted.
- workerd and WASI show that the resource boundary must be a value passed in, and that nothing is ambient.
- Node's own error codes answer the surface question without inventing behaviour.

## Proposed architecture

The runtime becomes a core plus a closed list of capability services. `embed.start` requires every one of them, and the embedder provides each with `Effect.provideMut`. Silk specializes a provided service into monomorphic code and has no interface objects, so the compiler knows the implementation at every call. The choice costs nothing at run time, and a constant in the implementation can remove code.

```
embedder ── embed.start(options, host)
              │  provides every service with Effect.provideMut
              ▼
core (always linked): engine, loop and immediates, bootstrap, loader, id registry,
                      every service contract and every empty implementation
              │
              ▼
capability services: Files · Network · Processes · SystemInfo · Workers · Crypto · Tls ·
                     Compression · Http · Http2 · Wasm · Database · TypeStripping ·
                     Inspector · NativeAddons · HostApi   (plus Platform, ModuleResolver: always real)
              each one: a shipped real implementation │ the embedder's own │ Service.empty
```

Read top down: the embedder provides every service, the core is the only part that is always linked, and each service is given as a real implementation, the embedder's own, or the empty one.

### Capabilities as services

Each part of the runtime that an embedder can choose is one service. None can be left out: the embedder provides a real implementation, its own, or the empty one the runtime ships.

```silk
/// The file capability: the operations `node:fs` can ask for.
pub service Files {
  /// A constant of the implementation type. False means the capability is not there: its modules,
  /// globals and native functions are not registered, and the code behind them is not linked.
  fn present(self: &Self) -> bool
  /// True when operations may block, so asynchronous calls go to the blocking pool.
  fn blocks(self: &Self) -> bool

  effect fn open(self: &Self, path: &[u8], flags: OpenFlags, mode: u32) -> Descriptor ! FileError
  effect fn readAt(self: &Self, file: Descriptor, into: &mut [u8], offset: u64) -> usize ! FileError
  effect fn stat(self: &Self, path: &[u8], follow: bool) -> Metadata ! FileError
  // ... write, close, links, directories, watch
}

/// The empty implementation, declared beside the service and reached as `Files.empty`.
/// Its operations are never called, because `present` is false.
impl Files for EmptyFiles {
  fn present(self: &Self) -> bool { return false }
  fn blocks(self: &Self) -> bool { return false }
  effect fn open(self: &Self, path: &[u8], flags: OpenFlags, mode: u32) -> Descriptor ! FileError { return raise FileError.notSupported() }
  // ... every operation spelled out, since nothing is defaulted
}

// An embedder that wants no files says so:
//   |> Effect.provideMut<Files>(&mut Files.empty)
```

Every service has its `empty` under the same name (`Wasm.empty`, `Http2.empty`), so there is no second list of type names to learn or import.

The list of services:

| Service | Node surface that stands on it | Real implementations shipped | Empty implementation |
| --- | --- | --- | --- |
| `Platform` | The core: wall and monotonic time, entropy, waiting and waking the loop, the three standard streams, arguments, environment, working directory, exit | `OsPlatform`; `FixedPlatform` for tests and images | None. Always real |
| `ModuleResolver` | The loader | `NodeResolution`; `BuiltinsOnly` | None. Always real |
| `Files` | `fs`, and through it WASI preopens, SQLite's VFS, compile cache, reports and snapshots | `OsFiles` rooted at a directory; `MemoryFiles` | `Files.empty` |
| `Network` | `net`, `dgram`, `dns`, and through it `http`, `fetch`, `WebSocket` | `OsNetwork`; `NoEgress` for listeners only | `Network.empty` |
| `Processes` | `child_process` | `OsProcesses` | `Processes.empty` |
| `SystemInfo` | `os` | `OsSystemInfo`; `FixedSystemInfo` | `SystemInfo.empty` |
| `Workers` | `worker_threads` | `ThreadWorkers` | `Workers.empty` |
| `Crypto` | `crypto`, `crypto.subtle` | `OpenSslCrypto` | `Crypto.empty` |
| `Tls` | `tls`, `https` | `OpenSslTls` | `Tls.empty` |
| `Compression` | `zlib`, compression streams | `NativeCompression` (zlib, zstd, brotli) | `Compression.empty` |
| `Http` | The HTTP/1 parser under `http` | `Llhttp` | `Http.empty` |
| `Http2` | `http2` | `Nghttp2` | `Http2.empty` |
| `Wasm` | `WebAssembly`, and `wasi` when `Files` is present too | `WamrWasm` | `Wasm.empty` |
| `Database` | `sqlite`, `localStorage` | `Sqlite` | `Database.empty` |
| `TypeStripping` | Running TypeScript files | `Amaro` | `TypeStripping.empty` |
| `Inspector` | `inspector`, profiler, coverage, tracing | `NodeInspector` | `Inspector.empty` |
| `NativeAddons` | Node-API, `process.dlopen` | `DlopenAddons` | `NativeAddons.empty` |
| `HostApi` | The embedder's own native functions and modules | The embedder's type | `HostApi.empty` |

**How an empty service removes code.** Two things work together. The C library is a dependency of the package that holds the real implementation, so a program that never names `WamrWasm` never compiles WAMR. And the runtime's own code for the capability (registering its native functions and built-in JavaScript, its state, its turn in the loop) sits under `if wasm.present()`. The provided service is specialized, so for `Wasm.empty` that condition is the constant false and the code behind it is not linked. The engine already does this for Intl: `EngineServices.includesIntl` returning false removes the Intl code and its CLDR tables. Step 1 proves it for Wasm with a symbol check on the binary.

**Two depths of contract.** The first seven services in the table describe a resource in terms of operations, as `Files` does above. The Node surface over them is runtime code in the core, and an embedder can reasonably write its own implementation. The rest wrap a library. `crypto_native` is 135 KB written against OpenSSL, so a library-neutral `Crypto` contract cannot be the first step. These services start with the contract below, which is what `host.silk` and `worker.silk` spell out per extension by hand today, and the package of the real implementation carries the native glue and the built-in JavaScript together with the library. An embedder picks the shipped implementation or the empty one. When a vendored library is replaced (step 8), its service narrows to operations, and `silk.crypto` becomes one more implementation of `Crypto`.

```silk
/// What a library service answers until its contract narrows. Every method is required.
pub service Binding {
  fn present(self: &Self) -> bool
  /// Its native functions: bootstrap name and id, inside the id block registered for the service.
  fn natives(self: &Self) -> &[Native]
  /// Its built-in JavaScript: name, embedded source, public or internal, preloaded into an image or not.
  fn builtins(self: &Self) -> &[Builtin]
  /// The ids Node's permission policy is asked about before the call.
  fn guarded(self: &Self, id: u32) -> bool

  effect fn open<'m, S>(self: &mut Self, engine: &mut Engine<'m, S>, realm: Realm) -> () ! OutOfMemoryError | JsException
  effect fn call<'m, S>(self: &mut Self, engine: &mut Engine<'m, S>, realm: Realm, id: u32, arguments: Arguments) -> ValueBits ! OutOfMemoryError | JsException
  /// The loop's poll phase: deliver completions. True when something ran.
  effect fn poll<'m, S>(self: &mut Self, engine: &mut Engine<'m, S>, realm: Realm) -> bool ! OutOfMemoryError | JsException
  /// Referenced work that keeps the loop alive, and what a leak report lists.
  fn openHandles(self: &Self) -> usize
  effect fn close<'m, S>(self: &mut Self, engine: &mut Engine<'m, S>, realm: Realm) -> ()

  /// The engine's hooks for objects of its classes: exotic property access, trace, finalize, buffer release.
  /// (One method per EngineServices hook that host.silk routes by id today.)

  /// A fresh instance for a worker thread.
  effect fn forWorker(self: &Self) -> Self ! OutOfMemoryError
  /// After a heap image is mapped: reopen native state the image cannot hold.
  effect fn hydrate<'m, S>(self: &mut Self, engine: &mut Engine<'m, S>, realm: Realm) -> ()
}
```

**The list is closed.** A new capability in the runtime is a new service in the list, and every embedder then fails to compile until it names an implementation. The host's own native functions and modules go through the one slot made for them, `HostApi`, which replaces today's `Embedder` struct and its C function pointer. A third party cannot add a service from outside. That is the trade against a free list of extensions, and it buys an entry point with no set to assemble.

**Dispatch.** `Services.nativeGeneric` stays an if-chain over constant id ranges, with each arm under its service's constant `present`. The calls the loop makes per callback (immediates 2230 to 2241, the clock, `bufferOperation`) stay in the core ahead of the chain, as today. Id blocks stay fixed numbers in one registry file in the core, because bytecode caches and heap images store them, and a block belongs to a service, not to an implementation. The gate is equal instruction counts on Octane and the HTTP benchmarks.

**Services that need each other.** `Tls` needs `Network` and `Crypto`. `Http2` and `Inspector` need `Network`. `TypeStripping` needs `Wasm`, because amaro runs on WebAssembly. `localStorage` needs `Database` and `Files`. `embed.start` checks the `present` constants in a static assertion, so a wrong combination fails the build with a message naming both services (`checkCombination`, step 5).

**The one dependency of this shape.** Native functions reach a service as an Effect requirement, and they are called through the engine's host hooks, whose requirement is fixed at `? &mut Allocator` today. The hooks have to carry a generic requirement row. Step 1 measures that on Octane instruction counts before anything else moves. If it costs, the same services become the fields of one `Capabilities` struct passed to `embed.start`. The embedder then writes a field per service instead of a `provideMut` line per service, and nothing else in this design changes.

### Rules for implementations

- An operation is synchronous and may block. The runtime runs it on the blocking pool for asynchronous calls and inline for `Sync` calls, as `fs_async` does now. `blocks` returning false lets `MemoryFiles` run on the loop thread without a pool hop.
- Operations take `&Self` and are called from pool and worker threads. Sharing and locking are the implementation's business.
- Where the Silk standard library has the interface, the service is defined in its terms: `Random`, `SystemClock`, `MonotonicClock`, `ChildProcess`. `silk.system.filesystem.FileSystem` is whole-file (read, write, stat, create, remove), so `Files` is new. Its descriptor-level operations are a candidate to upstream, so std's `FileSystem` and Node's share one native implementation.
- `Network` starts as a policy and factory over real sockets (`silk.net` connections registered with the reactor), which covers allowlists, egress control and pre-opened listeners. A byte-level virtual transport over `silk.io.byte_duplex` is a later addition if an embedder needs to serve requests without sockets.
- After the migration, only packages of real implementations may declare `extern "C"` or import a `*_native` standard module. A CI script enforces it. That is what makes "no file system" true instead of approximately true.

### Build time and run time

**Build time is the package graph.** The module becomes several Silk packages. An embedder's `silk.toml` lists the core and the packages of the real implementations it provides, and a C library is compiled only when its package is a dependency.

| Package | Holds | Native code it brings |
| --- | --- | --- |
| `silkjs` (engine) | Unchanged. The runtime stops importing its `js_std` and `js_os` libc modules | None |
| `silkjsruntime-core` | `Runtime`, the loop and immediates, bootstrap, loader, every service contract and every empty implementation, the registry of id blocks, the Node surface over the resource services (`fs`, `net`, `dgram`, `dns`, `child_process`, `os`, `worker_threads`, fetch, WebSocket), and the JavaScript with no outside dependency: `events`, `buffer`, `util`, `path`, `url`, `stream`, `timers`, `assert`, `console`, `string_decoder`, `querystring`, `async_hooks`, `diagnostics_channel`, `vm`, `module`, `process` | None |
| `silkjsruntime-os` | `OsPlatform`, `OsFiles`, `OsNetwork`, `OsProcesses`, `OsSystemInfo`, `ThreadWorkers`, the reactor and blocking pool | libc; libuv until the loop plan removes it |
| `silkjsruntime-memory` | `MemoryFiles`, `FixedPlatform`, `FixedSystemInfo`, bundle module storage | None |
| `silkjsruntime-openssl` | `OpenSslCrypto`, `OpenSslTls` | OpenSSL |
| `silkjsruntime-compress` | `NativeCompression` | zlib, zstd, brotli |
| `silkjsruntime-llhttp`, `-nghttp2` | `Llhttp`, `Nghttp2` | llhttp, nghttp2 |
| `silkjsruntime-wamr` | `WamrWasm`, with WASI | WAMR, SIMDe |
| `silkjsruntime-sqlite` | `Sqlite` | SQLite |
| `silkjsruntime-amaro` | `Amaro` | None (3.8 MB of JavaScript) |
| `silkjsruntime-inspector`, `-napi` | `NodeInspector` with profiler, coverage and tracing; `DlopenAddons` | `inspector_transport.c` until ported; `dlopen` |
| `silkjsruntime` | The executable: the real implementation of every service, the command line, REPL, test runner, SEA, package manager | Everything |

**Run time is values.** Which directory, which environment variables, which hosts, which standard streams: all are values of the implementations the embedder constructs. One rule keeps this simple: presence never varies at run time, because `present` is a constant of the type. A host that serves tenants with different surfaces calls `embed.start` with different implementation types in one binary, and each combination is its own specialization with its own image.

**Package parameters are for scalars.** Silk's `pub param` with `[config]` in the manifest fits knobs that are values, not surface: debugger hooks compiled into the dispatch loop, the CLDR data tier for Intl, default heap limits. They are not used to switch modules, because a boolean per module cannot express "my own file system" and multiplies untested combinations. Module-level `static if` is listed in the Silk reference as a confirmed contract with implementation tracked separately, so it should be verified on the pinned toolchain before any use.

An embedder then reads like this:

```silk
// A sandboxed plugin host: its own file system, outbound HTTPS to chosen hosts, no Wasm, no child processes.
return run embed.start(move options, &mut host)
  |> Effect.provideMut<Platform>(&mut platform)            // FixedPlatform: chosen arguments, environment, output
  |> Effect.provideMut<ModuleResolver>(&mut plugins)       // the host's own: a table of plugin modules
  |> Effect.provideMut<Files>(&mut tenantFiles)            // the host's own
  |> Effect.provideMut<Network>(&mut allowlist)            // the host's own, over OsNetwork
  |> Effect.provideMut<Crypto>(&mut crypto)                // OpenSslCrypto
  |> Effect.provideMut<Tls>(&mut tls)                      // OpenSslTls
  |> Effect.provideMut<Http>(&mut http)                    // Llhttp
  |> Effect.provideMut<Compression>(&mut compression)      // NativeCompression
  |> Effect.provideMut<HostApi>(&mut api)                  // the host's own functions
  |> Effect.provideMut<Http2>(&mut Http2.empty)
  |> Effect.provideMut<Processes>(&mut Processes.empty)
  |> Effect.provideMut<SystemInfo>(&mut SystemInfo.empty)
  |> Effect.provideMut<Workers>(&mut Workers.empty)
  |> Effect.provideMut<Wasm>(&mut Wasm.empty)
  |> Effect.provideMut<Database>(&mut Database.empty)
  |> Effect.provideMut<TypeStripping>(&mut TypeStripping.empty)
  |> Effect.provideMut<Inspector>(&mut Inspector.empty)
  |> Effect.provideMut<NativeAddons>(&mut NativeAddons.empty)
  |> Effect.provideMut<Allocator>(&mut allocator)
```

The list is long on purpose: it is the whole surface of the program, readable in one place, and the empty lines say what the program cannot do. The executable is the same call with the real implementation on every line and `HostApi.empty`.

### Starting a simple case

The smallest embedder is a manifest with two dependencies and one file. It runs JavaScript with the language, timers, `console`, events, buffers, streams and URL, and with no file system, network or Wasm, because every capability service gets its empty implementation. This is a sketch of the proposed API, not code that builds today.

```toml
# silk.toml
[package]
name = "hello-embed"

[dependencies]
silkjsruntime-core = { git = "https://github.com/silk-lang/lib-silkjs", package = "silkjsruntime-core" }
silkjsruntime-os   = { git = "https://github.com/silk-lang/lib-silkjs", package = "silkjsruntime-os" }
```

```silk
// src/main.silk
import silk.mem.allocator { Allocator, OutOfMemoryError }
import silk.core.effect { Effect }
import silkjs.js_exceptions { JsException }
import runtime.core.embed { Host, Options, Runtime, Turn }
import runtime.core.embed
import runtime.core.services { Platform, ModuleResolver, Files, Network, Processes, SystemInfo, Workers, Crypto, Tls,
  Compression, Http, Http2, Wasm, Database, TypeStripping, Inspector, NativeAddons, HostApi }
import runtime.core.modules { BuiltinsOnly }
import runtime.os.platform { OsPlatform }

const SOURCE: &[u8] = b"setTimeout(() => console.log('hello from JavaScript'), 10)"

struct App {}

impl Host for App {
  // Called once the runtime is up. Evaluate a module, then turn the loop until it has nothing left.
  effect fn drive<'e, 'm>(self: &mut Self, runtime: &mut Runtime<'e, 'm>) -> i32 ! OutOfMemoryError | JsException ? &mut Allocator {
    run runtime.evaluateModule(b"/app/main.mjs", SOURCE)
    while true {
      match run runtime.turn(-1.0) { Turn.Running => () Turn.Finished { status } => { return status } }
    }
    return 0
  }
}

pub effect fn main() -> i32 {
  let mut allocator = Allocator.systemAllocatorProvider()
  let mut platform = OsPlatform.make()        // real clock, entropy, stdout and stderr
  let mut modules = BuiltinsOnly {}           // imports resolve to built-in modules only
  let mut app = App {}
  let mut options = run Options.make(b"hello") |> Effect.provideMut<Allocator>(&mut allocator)
  options.keepAlive = false                    // finish when the loop is empty, as Node does

  return run embed.start(move options, &mut app)
    |> Effect.provideMut<Platform>(&mut platform)
    |> Effect.provideMut<ModuleResolver>(&mut modules)
    |> Effect.provideMut<Files>(&mut Files.empty)
    |> Effect.provideMut<Network>(&mut Network.empty)
    |> Effect.provideMut<Processes>(&mut Processes.empty)
    |> Effect.provideMut<SystemInfo>(&mut SystemInfo.empty)
    |> Effect.provideMut<Workers>(&mut Workers.empty)
    |> Effect.provideMut<Crypto>(&mut Crypto.empty)
    |> Effect.provideMut<Tls>(&mut Tls.empty)
    |> Effect.provideMut<Compression>(&mut Compression.empty)
    |> Effect.provideMut<Http>(&mut Http.empty)
    |> Effect.provideMut<Http2>(&mut Http2.empty)
    |> Effect.provideMut<Wasm>(&mut Wasm.empty)
    |> Effect.provideMut<Database>(&mut Database.empty)
    |> Effect.provideMut<TypeStripping>(&mut TypeStripping.empty)
    |> Effect.provideMut<Inspector>(&mut Inspector.empty)
    |> Effect.provideMut<NativeAddons>(&mut NativeAddons.empty)
    |> Effect.provideMut<HostApi>(&mut HostApi.empty)
    |> Effect.provideMut<Allocator>(&mut allocator)
}
```

`silk build` then gives a binary with the engine and the core only. In it, `require('node:fs')` throws `ERR_UNKNOWN_BUILTIN_MODULE` and `typeof fetch` is `'undefined'`.

Adding a capability is one changed line, plus one dependency when the implementation brings a library. Files limited to a directory:

```silk
import runtime.os.files { OsFiles }

let mut files = run OsFiles.make("/srv/hello/data")   // JavaScript sees this directory as /

  // in the call to embed.start, in place of the Files.empty line:
  |> Effect.provideMut<Files>(&mut files)
```

Deleting a line is a compile error, since `embed.start` requires every service. Replacing `OsFiles` with `MemoryFiles`, or with a type of the embedder's own that implements `Files`, changes nothing else. WebAssembly is the same step with a dependency: add `silkjsruntime-wamr` to the manifest and provide `WamrWasm` in place of `Wasm.empty`. If the step 1 measurement rules out requirement rows through the engine, the same choices are written as fields of one `Capabilities` struct, and the rest reads the same.

### One product: the runtime re-exports the engine

The engine and the runtime are offered as one product under one name, and the runtime package re-exports the engine's embedding API. Someone who wants only the engine depends on the same product and imports the engine modules through it; someone who wants Node surface provides services as above.

The design is unchanged by this: the core stays always linked when the runtime is used, and the engine stays its own Silk package underneath, since the runtime depends on it and it also builds to wasm32. Two things follow:

- The core package lists the engine's public modules in its `exports` (or re-exports them from one `engine` module), so one dependency line gives both. A program that imports only the engine modules links only the engine, because nothing of the runtime is reachable.
- Documentation and naming present one product with two entry points: the engine API for running JavaScript with nothing around it, and `embed.start` with its services for a runtime.

Splitting the core itself into services, so that providing every empty implementation would give the bare engine, was considered and set aside as more machinery than the naming problem needs.

### Custom module resolution

Module resolution becomes a required service of its own, `ModuleResolver`, separate from module storage. The embedder provides one at start like any other service, and the runtime ships `NodeResolution`, which is today's algorithm.

```silk
pub service ModuleResolver {
  /// Turns a specifier into what to load. `kind` is import, require or the entry point;
  /// `conditions` are the active export conditions.
  effect fn resolve(self: &Self, specifier: &[u8], referrer: &[u8], kind: RequestKind, conditions: &[Bytes]) -> Resolution ! ResolveError
  /// Gives the module's code for a resolved location.
  effect fn load(self: &Self, location: &[u8]) -> Loaded ! LoadError
}

pub union Resolution {
  /// A built-in of the core or of a service that is present.
  Builtin { name: Bytes },
  /// A location for `load`, with the module format when the resolver already knows it.
  Module { location: Bytes, format: Option<Format> },
}

pub union Loaded {
  Source { text: Bytes, format: Format },      // ESM, CommonJS, JSON, TypeScript, Wasm
  Bytecode { bytes: Bytes },                    // compiled ahead with the same build
}
```

Three levels of customization result, from least to most code:

| Level | Who writes it | Typical use |
| --- | --- | --- |
| Storage only | Embedder gives `NodeResolution` over its own module storage (a directory, a memory bundle, its file system provider) | Node rules, but modules come from a bundle or a virtual tree |
| Wrap the Node resolver | A type of the embedder's that holds a `NodeResolution`, answers some specifiers itself and passes the rest on | Virtual modules (`app:config`), aliases and import maps, pinning versions, refusing bare specifiers or `node:` modules, serving precompiled bytecode |
| Replace it | A type that implements `ModuleResolver` alone | A host with no `node_modules` at all: a fixed module table, URL imports, a plugin registry |

Notes on fit with what exists:

- The engine already asks the runtime to normalize and load every import (`Services.normalize` and `load`, answered by `__silkRuntime` in `bootstrap.js`), and `libs/node_resolver/loader.js` holds the Node algorithm in JavaScript. `NodeResolution` is that code behind the service, so the executable's behaviour does not change.
- Node's own customization hooks (`module.register`, `module.registerHooks`, in `hooks.js`) stay and run inside `NodeResolution`. They are the JavaScript-level way to customize; the service is the native one, and it runs first, so a script cannot undo what the embedder's resolver refuses.
- The resolver is required, with no default, in line with the rule for every other service. The executable names `NodeResolution` explicitly.
- It lands in step 3, with the platform's module storage, since both replace the loader's direct file access.

## The Node surface when a capability is absent

A module whose service has the empty implementation does not exist, and the runtime says so the way a Node build without that capability does. The runtime generates no stub modules.

An embedder has three explicit choices per capability, and each has one behaviour:

| Choice | Example | What JavaScript sees |
| --- | --- | --- |
| Real implementation | `OsFiles` rooted at `/srv/app` | Node behaviour inside the root |
| Virtual or denying implementation | `MemoryFiles`, or a type of the embedder's whose operations fail | The module loads and works against that implementation. A denied operation fails with the errno it returns (`EACCES`, `ENOSYS`), in Node's uv error shape |
| Empty implementation | `Files.empty` | The module is absent, as below |

The second row is the answer for packages that `require('fs')` at load and never call it. The embedder provides `MemoryFiles`, which is what workerd does. That is an implementation chosen by the embedder, not a shim hidden in the runtime.

With the empty implementation:

| Surface | Behaviour | Precedent |
| --- | --- | --- |
| `require('node:X')`, `import 'node:X'` | Throws `ERR_UNKNOWN_BUILTIN_MODULE`. For crypto: `ERR_NO_CRYPTO`. Inspector entry points: `ERR_INSPECTOR_NOT_AVAILABLE` | Node built `--without-ssl` or `--without-inspector`; all three codes are already in `internal/errors.js` |
| `module.builtinModules`, `module.isBuiltin`, `process.moduleLoadList` | List only what is present | Node lists what the build has |
| `process.features`, `process.versions`, `process.config.variables` | Report what is present: no `openssl` version with `Crypto.empty`, `features.inspector` false, and so on | Node |
| Globals | Not defined: `WebAssembly` with `Wasm.empty`; `fetch`, `WebSocket`, `EventSource` with `Network.empty`; `localStorage` with `Database.empty`; `process.stdin` without a platform stream | `typeof WebAssembly === 'undefined'` is the standard feature test |
| `globalThis.crypto` | `getRandomValues` and `randomUUID` are core, over the platform's entropy. `subtle` exists only when `Crypto` is present | Split by dependency, since the two halves need different things |
| Command line flags for an absent capability (`--inspect`, `--experimental-wasm-*`) | Rejected as unknown options | Node without the inspector |
| A built-in that needs an absent service | Cannot happen: the check on the `present` constants in `embed.start` refuses the combination | Deno's `deps` |

The last row is where the work is. Built-in JavaScript requires across capability lines freely today, mostly lazily inside functions. Three cases need a decision in the design, not a guard at run time:

- **Hard dependency.** `https` needs `tls`, `tls` needs `net` and `crypto`, `localStorage` needs SQLite, TypeScript stripping needs WebAssembly. These are the pairs `embed.start` checks.
- **Core module using a capability for one feature.** `console` writing to a TTY, `util.inspect` of a `KeyObject`, `stream` helpers that import `fs`, `process.stdout` being a `net.Socket` or `fs.WriteStream`. The core side takes the feature through a registration the capability's JavaScript makes when it loads (it adds its inspect formatter, its stream kind), so the core never names the capability. The platform's output stream is a plain writable in the core, and a present `Network` or a terminal platform stream upgrades it.
- **Convenience import.** A module that requires another only for a validator or constant moves that piece to the core.

A build-time check enforces the result: a script reads every built-in's `require` and `import` strings and fails when a file of one service names a module of another that is not among its declared needs. It replaces review as the guard, the same way `seal()` guards environment reads in the image build.

Node's own test suite runs against the build with every service real. Builds with empty services get their own smaller suite that asserts absence (the error codes above, the missing globals, no symbols of the dropped library in the binary).

## Binary size, cold start and the heap image

Size and startup both become functions of the implementations the embedder provides, and the heap image has to become one too, or embedders keep paying a cold bootstrap.

**Size.** No per-extension numbers exist yet, so none are claimed here. What is known from project notes: the executable is about 71 MB before the image is appended and about 115 to 120 MB with it, the first build compiles about 1,300 C files, and the built-in JavaScript is 11.4 MB of source across 561 files before bytecode. The expected order of savings, to be confirmed by measurement in step 0, is OpenSSL first, then built-in JavaScript and its bytecode (amaro alone is a third of the source), then SQLite, WAMR, the three compression libraries, nghttp2, libuv and llhttp. Step 0 produces the table from the linker map: bytes of text and data per archive and per `ext` module, and bytes of JavaScript and bytecode per extension.

Two things beyond dropped libraries shrink a reduced build. Each engine hook that `host.silk` routes to an extension today keeps that extension reachable, so an empty service removes its Silk code as well as its C. And the image only holds modules of services that are present.

**Cold start.** From the cold start work: an empty script takes about 10.3 ms without the image and 5.7 ms with it, a new page costs about 2 microseconds however it arrives, and a cold image dirties about 540 pages. A smaller set helps in three ways: fewer native functions defined and fewer states opened in `configure` (napi and the inspector open at every start today), fewer built-ins registered, and a smaller image with fewer pages to touch. The full set must not get slower: gates are the existing cold start harness and equal instruction counts for the bootstrap.

**The heap image today** is tied to the executable in four ways: it is written by running the linked binary with `--silk-write-heap-image`, appended by `tools/build.py` with a trailer, mapped at a fixed address, and valid only for that exact binary because it holds code addresses. Built-in bytecode is produced the same way (`--silk-write-builtin-cache`, then a relink). Embedders get neither.

**The heap image in the new design:**

1. The two-stage build moves from `tools/build.py` into a build step of the core package that any dependent package can run for its own program: link, run the program's image entry, append. The embed API gains `embed.writeImage(options, path)`, which requires the same services as `embed.start`, and `embed.start` looks for the trailer in its own executable, so an embedder's binary gets an image by adding one task.
2. The image is built with `FixedPlatform`, never the OS one. Today the build guards against the bootstrap reading the environment (`seal()` fails on an unlisted read). With a platform service the guard is structural: there is no environment to read while the image is built, and the real platform takes over at start.
3. Each service declares which of its built-ins preload into the image (`Builtin.image`), replacing the `imageModules` list in `bootstrap.js`. `net` and `http` are in that list today, so the core cannot build an image without them until this moves.
4. Each service reopens its own native state after the image is mapped (`hydrate`). This replaces the by-name reopen code in `worker.silk` and the `host.onHydrate` registrations.
5. The image identity already hashes the compiler, the JavaScript and the sources. It additionally covers the implementation type given for each service, so an image built for one combination is refused by another.
6. A program that cannot run on the build machine (a cross build) has no image and no bytecode cache and bootstraps cold. That is today's behaviour for `silk build`, and it stays the documented fallback.

A runtime with the profile-guided image layout (page packing from pagemap data, startup function ordering) keeps those per combination: the ordering files are inputs of the build step, produced by the same tasks against the embedder's binary.

## Migration plan

Eight steps, each merged to main with the executable's behaviour unchanged. The standing gates apply to every step: the Node suite and the 72 differential cases do not regress, Octane and HTTP instruction counts stay equal, and cold start does not get slower.

0. **Measure and draw the lines.** Produce the size table from the linker map, and a dependency graph of built-in JavaScript (every `require` and `import` string, marked load-time or lazy) plus which `host.*` natives each file uses. Fix the service boundaries and which services need each other from that data. Output: a report in project files. No code changes.
1. **Services through the engine, proven on Wasm.** First the spike: thread a generic requirement row through the engine's host hooks and compare Octane instruction counts. That decides whether services arrive as Effect requirements or as fields of one `Capabilities` struct. Then add the `Wasm` service with `WamrWasm` and `Wasm.empty`, move WAMR out of the runtime manifest into `modules/native/wamr` and a `silkjsruntime-wamr` package, and put the Wasm parts of `host.silk`, `worker.silk` and the bootstrap under `present`. Everything else stays wired as it is. The embedding smoke test gains a second build with `Wasm.empty`, and CI checks with `nm` that it has no WAMR symbol and that `typeof WebAssembly` is `'undefined'`. The `Embedder` function pointer becomes the `HostApi` service in the same step.
2. **Built-in JavaScript per service.** `tools/embed.py` writes one index per service. The bootstrap registers the built-ins of services that are present, and `builtinModules`, `process.features`, the loader's errors and the command line options follow it. The cross-service `require` check goes into CI. Amaro moves behind `TypeStripping`.
3. **`Platform` and `ModuleResolver`.** Time, entropy, waiting, standard streams, arguments, environment, working directory and exit go through `Platform`. The bootstrap stops spreading `silk:host/std` and `silk:host/os` into `host`, and the loader resolves and reads through `ModuleResolver`. Output: an embedding smoke test with `Files.empty` that runs a bundled module graph from memory.
4. **`Files`.** `fs_native`, `fs_async`, `fs_ops` and `uv_fs` are rewritten over the `Files` service, with `OsFiles`, `MemoryFiles` and `Files.empty`. WASI preopens, SQLite's VFS, the compile cache, reports, snapshots, the DNS resolver configuration and TLS root loading route through it. Output: smoke tests for a rooted directory and for an embedder-defined file system.
5. **`Network` and what stands on it.** `Network`, then `Tls`, `Crypto`, `Http`, `Http2` and `Compression`, each with its empty implementation. Fetch and WebSocket follow `Network`. The core gets the small hashes it needs from `silk.crypto` so it does not reach OpenSSL.
6. **The rest.** `Workers`, `Processes`, `SystemInfo`, `Database`, `Inspector` with profiling, coverage and tracing, `NativeAddons`. When the last one moves, `Services` has no state pointer per extension left, and `host.silk` only routes id blocks to services.
7. **Packages, image, enforcement.** Split the module into the packages of the table above. Move the image and bytecode pipeline into a build step embedders can run, with the implementation types in the image identity. Turn on the rule that only packages of real implementations touch libc. CI builds a matrix: everything real, everything empty, `Wasm.empty`, `MemoryFiles`, each with its absence assertions and its own cold start number.
8. **Replace vendored libraries behind stable seams.** With each library private to one implementation package, the standing goal of removing vendored code becomes a series of local swaps: llhttp for `silk.http`, zlib, zstd and brotli for `silk.compress`, OpenSSL for `silk.crypto` and `silk.tls`, libuv out of `silkjsruntime-os` as the loop plan finishes. Each swap is a new implementation of the same service, and it is when a library service narrows from a binding to operations.

Steps 1 to 3 are sequential. After step 3, steps 4, 5 and 6 can run as parallel streams, since each service owns different files and code that has not moved compiles unchanged. Step 7's package split is last on purpose: boundaries are cheap to move inside one package and expensive across several.

## Progress

**Step 0.** `silk task graph` in `tools/capabilities` writes [capability-graph.md](capability-graph.md)
and [capability-crossings.txt](capability-crossings.txt) from the sources. They give the service of
each built-in, native function and Silk module, bytes per service, and the 240 requires and native
calls that cross a service line. `--sizes` adds linked sizes from a `tools/build.py` build. The
rules are data in `tools/capabilities.toml`, which `tools/embed.py` also reads to tag each built-in.
CI (`.github/workflows/capability-graph.yml`) runs `--check`, which fails on a new crossing. Service
boundaries are a first cut by name and are not fixed yet.

**Step 2, first part.** Every built-in carries its service (`builtinService`, from
`tools/capabilities.toml` through `tools/embed.py`), and the native side lists the services this
runtime has (`native.services`, every one for now). The bootstrap registers only the built-ins of
present services, so `builtinModules`, `isBuiltin` and the web globals follow presence. Requiring a
missing one fails as in a Node build without it: `ERR_UNKNOWN_BUILTIN_MODULE` for `node:X`, a
package lookup for a bare name, `ERR_NO_CRYPTO` for crypto and TLS, and
`ERR_INSPECTOR_NOT_AVAILABLE` for the inspector. `process.features` and that capability's command
line options follow presence too. With every service present the executable behaves as before.
TypeScript follows `TypeStripping`: without it a `.ts` file and `module.stripTypeScriptTypes`
throw `ERR_NO_TYPESCRIPT`, `process.features.typescript` is false and `node_use_amaro` is false, as
in a Node build without amaro. Two parts of step 2 move to the step whose service they need:
`globalThis.crypto` split between core randomness and `subtle` comes with the `Crypto` service
(step 5), since only then can a runtime be without Crypto; one index of built-ins per service
rather than one tagged index comes with the package split (step 7), since only then can a
service's JavaScript leave the program with its package.

**Step 1, the spike.** Neither route in "The one dependency of this shape" works on the then-pinned
toolchain (0.1.0-dev.74) as written:

- *Requirement rows through the engine hooks* put a row parameter on every signature that reaches
  a hook: 4,493 `? &mut Allocator` rows in all 474 engine files.
- *A generic services type*, `Services<C>` with `C` the embedder's capabilities, is cheap in the
  runtime. Only 223 references in 12 files name `Services`, the `ext` modules are mostly generic
  over the engine's services already, and the engine already specializes per services type, so
  dispatch would cost nothing. But the runtime has 266 `export "C" fn` callbacks (WAMR imports,
  SQLite user functions, every Node-API function, llhttp, OpenSSL, the worker entry handed to
  `pthread_create`), and each recovers the engine from a C pointer as one concrete type. Silk
  cannot take a C callback address of a generic instantiation (synchronous-native-callbacks.md,
  "Exported addresses"), and it has no type-erased generic values (CALLABLE-001).

What does work, as probes on the pinned compiler confirmed: an interface's `static fn present()`
read under `static if` removes the absent arm before name resolution, in debug builds too, and
`compileError` there can carry the dependency check. A forwarding interface over one
`Capabilities<W, A>` struct keeps the runtime generic over a single type however many capabilities
there are. Dependency C sources are linked as loose objects, so an empty capability leaves its
library out only when the package holding it is not in the program's dependency graph.

**Step 1.** The compiler change is [silk-lang/silk#3113](https://github.com/silk-lang/silk/pull/3113):
a generic export's name with type arguments is the C address of that instantiation, so a callback
is written once and handed over for the runtime's own type. On that compiler the runtime is generic
over the embedder's capabilities, `Services<C>` with `C: Composition` (`libs/core/capabilities.silk`):

- `Capabilities { wasm, hostApi }` was the closed list then (step 3 adds `platform` and `modules`); `Composition`, implemented by it alone,
  forwards each capability's operations, so every signature carries one type parameter however
  many capabilities there are.
- `Wasm` has `WamrWasm` (`./wamr`, the package that depends on WAMR) and `EmptyWasm` (the core).
  Presence is `Wasm.present`, a static function read under `static if`, so with `EmptyWasm` the
  Wasm natives, classes and loop turns are not compiled. The bootstrap sees no `wasmNative`:
  `typeof WebAssembly` is `'undefined'` and the Wasm built-ins are not registered.
- `HostApi` replaces the `Embedder` struct and its C function pointer.
- The C callbacks that re-enter the engine (SQLite, Node-API's bridge, TLS, the HTTP parser,
  WAMR's imports, the worker thread entry) are generic exports instantiated where the runtime
  hands them over. The HTTP parser's C bridge now takes its callback as a pointer instead of
  calling a fixed symbol.
- The executable moved to `./cli`; `./smoke` is the embedding smoke test without WebAssembly.

The smoke test with `EmptyWasm` links no WAMR symbol (`silk task symbols ... --absent`) and prints
`wasm undefined`; with `WamrWasm` it prints `wasm object`. The workspace is pinned to
0.1.0-dev.78, the first release with #3113, and SQLite's package moved with it
(silk-lang/lib-sqlite, the same branch name as this one).

One gap in the compiler remains: Silk emits a C export of a dependency package only where Silk
code takes it as a value, while the reference (FFI-010) says every export of the loaded modules is
a root. The core takes most of its exports as values (`ext/keep_exports.silk`), but a function
with a callback parameter cannot be one, so the 15 Node-API functions that take a callback
(`napi_define_class`, `napi_wrap` and the others) are plain functions in the core, and
`cli/src/napi_exports.silk` declares them as the executable's C exports. An embedder that loads
addons declares the same until the compiler roots dependency exports.

**Step 3, `ModuleResolver`.** The loader asks the program's `ModuleResolver` for what it needs of
module files: the type of a path, its real path and its source (`moduleNative`, id 19000). The
resolution algorithm stays the loader's, so `require` and `import` resolve as Node's do against
whatever the resolver serves. `NodeResolution` reads the operating system's files, as before;
`MemoryModules` serves a module graph bundled into the program, with directories implied by the
paths and nothing else there. The embedding smoke test runs on `MemoryModules`: its main module
imports an ES module, requires a CommonJS package through its package.json, and gets
`ERR_MODULE_NOT_FOUND` for a module that is not there. A worker thread's capabilities are now made
from its parent's (`forWorker(self)`), so a worker sees the same bundled graph. A module the host
evaluates (`Runtime.evaluateModule`) now has the loader's `import.meta` (`url`, `filename`,
`dirname`), as the runtime's own entry module does. `Files.empty` comes
with step 4, when `fs` stands on `Files`; until then `fs` still reads the operating system.

**Step 3, `Platform`.** `process.env`, `process.cwd()`, `process.chdir()` and exit go through the
program's `Platform` instead of the engine's `silk:host/std` and `silk:host/os`: the bootstrap's
calls through `platformNative` (id 19100), and `process.env` through the environment the worker
natives keep, where the shared one (the main thread's, and a worker's with `SHARE_ENV`) is the
Platform's and a worker's own copy is made from it. `OsPlatform` is the process's own; `FixedPlatform` gives the runtime an environment
and working directory of the embedder's, which it cannot change (`EPERM`). The smoke test runs on
`FixedPlatform` and sees only its one variable. Native code that reads the environment itself, and
child processes, still see the process's. What the bootstrap still takes from those two engine
modules is file access (`host.readFile`, `host.stat`, the permission model's lookups), the standard
streams' descriptors, `kill` and `getpid`, and `gc`: they move with `Files` (step 4), `Processes`
and `SystemInfo` (step 6). Time and entropy are the engine's (`EngineServices`) and stay there
until an embedder needs to fix them; `FixedPlatform` would then carry them.

**Step 4, first part: `Files`.** Every `fs` operation already went through one seam,
`ext/fs_ops.silk`'s `Call` and `Outcome`, run on the loop thread for a synchronous call and on a
blocking thread for an asynchronous one. That seam is now the `Files` capability (ids 2000 to
2007): `fs_native` and `fs_async` run each call through the program's implementation, and a
blocking thread gets it as `runRequest` instantiated for the runtime's capabilities, with the
address of the runtime's own. `OsFiles` is the operating system's file system, as before.
`MemoryFiles` keeps files in memory behind a lock, shared by every thread of the runtime; the
embedder can seed it (`addFile`). Its descriptors start at 1000, and 0 to 2 stay the process's
standard streams. It has no links and keeps no owners, and a relative path is taken from `/`. The
read that skips the blocking threads (`preadv2` with `RWF_NOWAIT`) runs only for `OsFiles`, and an
abandoned open is closed through the capability. Some of `fs`, and the bootstrap's own reads, call
the engine's `os` module on files directly; with a file system other than the operating system's
(`Files.direct` false), the bootstrap points those calls at the capability as well. The embedding smoke test runs on `MemoryFiles`:
a seeded file, a write and an append, `readdir`, `stat`, `existsSync` of a host path (false), and
`fs.promises.readFile` on the blocking threads.

**Step 4, second part: `EmptyFiles` and `RootedFiles`.** With `EmptyFiles` the program has no
file system: `Files` is not among the services, so `node:fs` and the built-ins that stand on it are
not registered (`import('node:fs')` fails with `ERR_UNKNOWN_BUILTIN_MODULE`,
`process.getBuiltinModule('fs')` is undefined), fs's natives (2000 to 2003) are not registered and
their dispatch is not compiled, and the bootstrap's own reads of files fail with `ENOSYS`. The
built-ins that start without `fs` no longer need it at load: `constants`, the constants binding,
the TTY writer, `internal/net`, the compile cache and `Utf8Stream` load it when used, the loader
checks for a patched `fs` only when there is one, and `package_json_reader` takes its stat from the
`ModuleResolver`. The smoke test without WebAssembly now has no file system either; the one with
WebAssembly keeps `MemoryFiles`. `RootedFiles` is the operating system's files under one
directory: paths are taken from the root and `..` stops there, and `realpath` and `mkdtemp` hand
paths back without it. The rooting is lexical, so a link inside the tree that points out of it is
followed. Its descriptors are the system's (`Files.native`), but a path does not mean the same to
it as to the system (`Files.direct` is false), so the bootstrap sends the engine's `os` calls
through the capability, as for `MemoryFiles`. `src/files_tests.silk`
(`silk test --root src/files_tests.silk`) tests both.

Still to come in step 4: the readers that do not go through `fs` and open files in C: WASI's
preopens (WAMR's own file layer), SQLite's VFS, and OpenSSL's root certificates. A
`ModuleResolver` over a `Files` (so a rooted or in-memory file system also serves modules) is a
small addition when an embedder needs it.

**Step 5, first part: Compression, Http2, Crypto, Tls.** Each is a library service with a
presence constant and an empty implementation: `NativeCompression` and `EmptyCompression`,
`Nghttp2` and `EmptyHttp2`, `OpenSslCrypto` and `EmptyCrypto`, `OpenSslTls` and `EmptyTls`.
`Capabilities` is now `{ platform, modules, files, compression, http2, crypto, tls, wasm, hostApi }`.
Without one, its natives are not registered, its state is not opened, its loop turn and resource
report are not compiled, and its built-ins are not there; `require('crypto')` fails with
`ERR_NO_CRYPTO` and `globalThis.crypto` is undefined, as in a Node build without OpenSSL, and
fetch asks for no content encoding without compression. The built-ins of other services that
reached into these at load (`constants`, the constants binding, `stream/web`, the iterable stream
transforms, fetch's transport, WebSocket and `internal/ws`, `crypto`'s cipher lists, the option
lookup) load them when used. Their C exports stay kept (`ext/keep_exports.silk`): the C glue that
calls them is still linked until the package split (step 7). Their contract is presence for now:
the natives, state and poll of each still live in the core and move with their package in step 7.

"Services that need each other" is settled at build time: `checkCombination` runs a `static if`
with `compileError` for each rule, so a program that gives `Capabilities` a real `Tls` and an
empty `Crypto` does not build ("Tls needs Crypto: give Capabilities a real crypto (OpenSslCrypto),
or no TLS (EmptyTls)"). `silk check` does not report it, since it does not instantiate the
runtime; `silk build` does.

The smoke test without WebAssembly has none of the four; the one with it has all of them.

**Step 5, second part: Network and Http.** `OsNetwork` and `EmptyNetwork`, `Llhttp` and
`EmptyHttp`. Without the network `node:net`, `node:dgram`, `node:dns`, `node:http`, `node:tty`,
`fetch` and `WebSocket` are not there, and the network's natives (2400 to 2405), state, loop turn
and resource report are not compiled; without the HTTP/1 parser its native (11000) and state are
not. Standard output and error are then written synchronously, as a file's are (`process.stdout.isTTY`
still says whether it is a terminal), and standard input is read as a file when there is a file
system. `tty` is the network's now (a terminal stream is a socket of libuv's, as in Node), and the
message event classes that worker ports use are the core's. `checkCombination` has the rules the
graph declares: the network needs Http, and Tls, Http2 and Processes need the network.  The
inspector's own server needs the network as well (step 6).

**Step 6, first part: Database, NativeAddons and Processes.** `SqliteDatabase` and
`EmptyDatabase`, `DlopenAddons` and `EmptyAddons`, `OsProcesses` and `EmptyProcesses`, in the same
shape as the libraries of step 5. Without a database
`node:sqlite` and `localStorage` are not there, and the database needs `Files`
(`checkCombination`). Without addons `process.dlopen` fails with `ERR_DLOPEN_DISABLED`, as with
`--no-addons`, and Node-API's natives, state and loop turn are not compiled; the Node-API C
functions stay exported until the package split. Without child processes `node:child_process` and
`node:cluster` are not there, a runtime started by a parent does not connect to its IPC channel,
and the process executor stays: it serves signals (`process.on('SIGINT')`, the inspector's SIGUSR1)
and handle transfer, which every runtime has, until those move to the core.

**Step 6, second part: SystemInfo and TypeStripping.** `OsSystemInfo` and `EmptySystemInfo`:
the errno, signal and dlopen numbers that `constants`, dotenv and the socket binding need are the
core's now (`internal/os_constants`, which `os.constants` is), so without system information only
`node:os` is gone; its native stays, as it also answers the process's own facts (its executable's
path, the component versions). `Amaro` and `EmptyTypeStripping`: what step 2 made follow the
service is now the embedder's choice, and `checkCombination` checks that amaro has WebAssembly to
run on.

**Step 6, third part: Workers.** `ThreadWorkers` and `EmptyWorkers`. `worker_threads`, message
ports, `process.env` and the thread identities stand on the worker natives, which every runtime
has, so they stay; without worker threads `new Worker()` fails with `ERR_WORKER_INIT_FAILED` and
the native start of a thread is not compiled.

**Step 6, fourth part: Inspector.** `NodeInspector` and `EmptyInspector`. The inspector here is
the protocol: `node:inspector`, `--inspect` and its kind, the in-process sessions the REPL uses, the
debugger and the network and storage views. Tracing (`trace_events`), the console's hook and the
V8 profiles that use the engine directly (heap snapshots, `v8.takeCoverage`, `NODE_V8_COVERAGE`) are
the core's now, since the console, diagnostics channels, fs and http report to them at load.
Without the inspector its natives (15000 to 15032) and state are not compiled,
`require('inspector')` fails with `ERR_INSPECTOR_NOT_AVAILABLE`, `--inspect`, `--cpu-prof` and
`--heap-prof` are bad options, `process.features.inspector` is false, the REPL has no previews,
`vm` contexts are not listed, and `console.profile` and `console.context` do nothing, as in a Node
build `--without-inspector`. The inspector needs the network (`checkCombination`).

With this, every capability of the design is a service with an empty implementation.

**Step 7, first parts: the library packages.** A library service now has a contract of
operations, not only its presence: `open`, `call` for its native ids, and `poll`,
`appendResources`, `openHandles` and `close` where it has work in flight, each forwarded by
`Composition` (`compressionCall`, `http2Open` and so on), with the state inside the
implementation's value instead of a pointer field of `Services`. Its version for
`process.versions` comes from its own native. So the real implementation can live in a package
that depends on the core, as `WamrWasm` does in ./wamr:

| Package | Implementation | Brings |
| --- | --- | --- |
| ./compress (`silkjsruntime-compress`) | `NativeCompression` | zlib, zstd, brotli |
| ./http2 (`silkjsruntime-nghttp2`) | `Nghttp2` | nghttp2 |
| ./llhttp (`silkjsruntime-llhttp`) | `Llhttp` | llhttp and its bridge |
| ./sqlite (`silkjsruntime-sqlite`) | `SqliteDatabase` | SQLite (lib-sqlite) |
| ./openssl (`silkjsruntime-openssl`) | `OpenSslCrypto`, `OpenSslTls` | OpenSSL |
| ./inspector (`silkjsruntime-inspector`) | `NodeInspector` | the inspector's WebSocket server (its own sockets and thread) |
| ./amaro (`silkjsruntime-amaro`) | `Amaro` | amaro's JavaScript (3.8 MB) |
| ./wamr (`silkjsruntime-wamr`) | `WamrWasm` | WAMR, SIMDe |

The core's manifest no longer lists those libraries. The embedding smoke test checks with `nm`
that the program of the core alone defines no symbol of any of them, and that the program with
every capability defines one of each. Their C entry points are kept by the implementation's
`open` (`keepExports`), since a dependency's C exports are emitted only when taken as values.

Two core services reach OpenSSL without being its natives: the network runs the TLS sessions of
native sockets (2405), and a worker thread's start sets OpenSSL's providers up first. Both go
through C function tables the core defines and the implementation fills
(`ext/library_hooks`: `SecureOps` from `Tls.secureOps`, `ThreadSetup` from
`Crypto.threadSetup`), so the core names no OpenSSL symbol; the empty implementations give tables
that attach no session and set nothing up. A library's version is a call of its native with no
arguments, so ordinary calls pay nothing for it.

The inspector's server computes the WebSocket handshake's SHA-1 itself, so it needs no OpenSSL.
`NodeInspector` holds the protocol's natives and the server; the engine's hooks reach it through
`Inspector` (`compiledSource`, `markEntry`, `debuggerCallback`, `pumpCallback`). The debugger
metadata the engine keeps on code (`ext/inspector_debugger` and what it uses) stays in the core,
since `vm` and the loader defer sources whether or not there is an inspector.

Built-in JavaScript can belong to a package too. `tools/embed.py` writes the built-ins of a
service listed in its `PACKAGED` into that package's C (`amaro/amaro_sources.c`), and the core's
table (`builtin_sources.c`) keeps an empty entry for each, which a pointer the package's C sets
when the program loads fills. `Amaro.link`, called once at start, is what makes a program that
depends on the package link that file, so a program without it holds none of amaro's bytes.

The Embedding workflow (`.github/workflows/embedding.yml`) is the matrix's first two rows: the
program of the core alone and the program with every capability package, each with its
expected output and its symbol checks (`silk task symbols`). The executable is the third.

**What is left of step 7.**

- `DlopenAddons` stays in the core. Node-API is several hundred C functions that addons find by
  name, and Silk emits a dependency's C exports only when something takes them as values. The
  compiler change that makes every loaded dependency's exports roots is done in Silk and waits for
  a release; with it, Node-API moves to a package the way the others did, and
  `cli/src/napi_exports.silk` goes away.
- libuv stays in the core until the loop plan replaces it; `OsFiles`, `OsNetwork`,
  `OsProcesses`, `OsSystemInfo` and `ThreadWorkers` stay with it for now (a `silkjsruntime-os`
  package once libuv is gone, so that the core touches no libc).
- The heap image and the built-ins' bytecode are still written by `tools/build.py` for the
  executable only. Moving that into a build step any embedding program can run, with the
  implementation types in the image identity, is the remaining half of the step.

**Step 8, first swap: compression.** `SilkCompression` (./silkcompress) is `Compression` over
`silk.compress`, with no C library: the same handle and job protocol as `NativeCompression`, and
the encoders and decoders of the standard library in place of zlib, brotli and zstd. It has
deflate, zlib, gzip, their inverses and unzip with every flush, dictionaries and `params`,
multi-member gzip with zero padding and strict trailing-junk checks, brotli with its quality and
window, and Zstandard with its level, window, checksum, content size and pledged size; it fails
at creation for what `silk.compress` lacks (brotli and Zstandard dictionaries, brotli's large
window, Zstandard's worker threads). Its errors are zlib's, brotli's and zstd's codes and
messages, in zlib's order of checks.

`cli/src/main_silkcompress.silk` is the executable with it in place of `NativeCompression`, and
`tests/compression-differential.mjs` runs the same cases (`tests/compression-cases.cjs`: one-shot
and streamed, every format, levels, strategies, windows, flushes, `params`, reset, dictionaries,
corrupt, truncated and trailing input, async work, `CompressionStream`) under Node and a runtime.
Both executables agree with Node on all 94; compressed bytes may differ from zlib's but always
decode under Node, and a brotli format error keeps its code family and message but not always
its number, which depends on where the decoder notices the fault. The compatibility suite passes
with it as well. On 8 MB, debug builds, gzip and Zstandard run as fast as the C libraries and
brotli compresses about 2.6 times slower and decompresses faster. The CI job `silk-compression`
(`.github/workflows/embedding.yml`) builds that executable and runs the differential.

The other swaps follow the same path: llhttp with `silk.http`'s parser, nghttp2 with its HTTP/2,
OpenSSL with `silk.crypto` and `silk.tls`, each a new implementation package of the same
capability, chosen by the embedder.

## Risks and decisions taken

Decisions made here without asking, each reversible before step 1 lands:

- Each capability is a required service with a shipped empty implementation, as Julia asked. The earlier draft's single extension set written as a type is dropped. Silk's optional services, which would let an unwanted capability simply be omitted, were considered and set aside: Julia prefers the whole surface written out here, and an optional service that one signature forgets to forward goes absent with no error.
- The list of services is closed. The host's own API goes through `HostApi`, which replaces the existing `Embedder` hook. Nothing depends on that hook but the smoke test.
- Absent means absent, with Node's error codes. No stub modules.
- Presence is a constant of the implementation type, fixed at build time. Run time varies values only.
- Services arrive as Effect requirements if the measurement in step 1 allows it, and as fields of one `Capabilities` struct if it does not.
- Library services (crypto, TLS, Wasm and the like) start with the binding contract and narrow to operations when their library is replaced.
- `globalThis.crypto` is split: random in the core, `subtle` with the `Crypto` service.

Risks:

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Requirement rows through the engine | The host hooks are fixed at the allocator today, and the engine is 474 files written against that. Past notes record that small changes to the call path cost measurable Octane instructions | The spike is the first thing in step 1, gated on equal instruction counts. The fallback is the same services as fields of one struct |
| Dispatch cost | Any per-call check on the native path is expensive | Hot ids stay in the core ahead of the chain. The chain is static with constant ranges, and `present` is a constant, so no check survives into the binary |
| An empty service does not remove everything | The design relies on the compiler and linker dropping code and embedded JavaScript behind a constant false | Step 1 checks the binary with `nm` and compares sizes. If embedded JavaScript survives, each service's JavaScript index moves into the package of its real implementation |
| Compile time and code size | The runtime is specialized once per combination of implementations, and the engine is large | One combination per binary is the normal case. Measure build time in step 1. Binaries with several combinations pay per combination, which is stated in the embedder docs |
| A long list at the entry point | 18 services, so 18 lines even for hello world | Deliberate: the list is the program's whole surface. The docs give two starting points to copy, everything real and everything empty |
| JavaScript untangling is larger than the native work | 561 files written as one bundle, many lazy requires | Step 0 sizes it from data before committing. The CI check stops regressions as other threads keep porting Node modules |
| A rooted `OsFiles` is not a sandbox by itself | Symlinks and `..` races escape a path prefix check | `OsFiles` resolves beneath a directory descriptor (`openat2` with `RESOLVE_BENEATH` on Linux, component-wise `openat` with no-follow elsewhere). The docs state which guarantees hold per platform |
| Leaks around services | Node-API loads arbitrary native code, SQLite and OpenSSL open files themselves, the inspector opens a socket | Each is routed through `Files` or `Network` in steps 4 to 6, or is a service a sandboxing embedder gives the empty implementation. The libc rule in step 7 catches what review misses |
| Concurrent streams | Compat and performance threads edit `host.silk`, `worker.silk` and `bootstrap.js` daily | Each step moves one service and leaves the rest compiling unchanged, so other threads only meet the new shape when their area moves. Announce step 1 and step 2 to open threads before merging |
| Silk features assumed | Generic requirement rows on interface methods, a static assertion over service constants, package build steps | Step 1 exercises all three on the pinned toolchain. A gap becomes a compiler change, which is in scope for this project |

Open points the measurements in step 0 should settle: whether `Http` should need `Network` or accept any byte stream (which decides if an embedder can serve HTTP without sockets), whether 18 services is the right grain or some should merge (`Crypto` with `Tls`, `Http` with `Http2`), and how much of `process` belongs to the core against the `Processes` service.

## Sources

- Code read: `silk-lang/lib-silkjs` at `197416d` (`modules/runtime/silk.toml`, `src/libs/core/host.silk`, `src/runtime/worker.silk`, `session.silk`, `embed.silk`, `runtime/bootstrap.js`, `tools/embed.py`, `src/ext/*`), and `silk-lang/silk` main (`stdlib/silk/system`, `reference/compilation-profiles.md`, `reference/module-static-selection.md`, `reference/generics-interfaces-and-specialization.md`).
- [deno\_core `extension!`](https://docs.rs/deno_core/latest/deno_core/macro.extension.html)
- [workerd capabilities](https://www.mintlify.com/cloudflare/workerd/concepts/capabilities) and [Workers `node:fs`](https://developers.cloudflare.com/workers/runtime-apis/nodejs/fs/)
- [wasmtime `WasiCtxBuilder`](https://docs.wasmtime.dev/api/wasmtime_wasi/struct.WasiCtxBuilder.html)
- [Javy plugin `Config`](https://docs.rs/javy-plugin-api/4.1.0/javy_plugin_api/struct.Config.html)
- [Node C++ embedder API](https://nodejs.org/download/nightly/v27.0.0-nightly20261018f88a1d25e9/docs/api/embedding.md)
