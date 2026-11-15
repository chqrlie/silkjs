# lib-silkjs

SilkJS, a Node-compatible JavaScript runtime written in [Silk](https://github.com/silk-lang/silk),
and the SilkJS Engine it runs on, a native Silk port of QuickJS. The runtime owns the `silkjs`
command, the way `node` is the runtime and not V8.

| Module | Package | What it is |
| --- | --- | --- |
| [`modules/engine`](modules/engine) | `silkjs-engine` | The SilkJS Engine: the JavaScript engine, its embedding API and the `silkjs-engine` shell |
| [`modules/runtime`](modules/runtime) | `silkjs-runtime` | SilkJS, the Node-compatible runtime and its `silkjs` command; depends on the engine by path |
| [`modules/pm`](modules/pm) | `pm` | The package manager, in Silk on the Silk standard library; it will run as the runtime's `install`, `add` and related commands |

The two are released together. The root [`silk.toml`](silk.toml) is a Silk workspace whose
`silk-version` pins the one toolchain both modules build with, overriding their own pins; move
them all with `silk set <version>` at the root. Both packages share one `version`, kept equal by
hand. `silk build` writes compiled packages to the workspace's `build/` at the repository root.

Each module is its own Silk package with its own `silk.toml` and tasks. Run tasks from the
module's directory:

```sh
silk use              # install the pinned Silk release
silk check --workspace

cd modules/engine
silk task             # list the engine's tasks
silk task check       # the correctness gate (needs `silk build --release`)

cd ../runtime
silk task build       # the runtime executable, modules/runtime/build/silkjs
silk task compat      # the differential cases against Node 24
```

The runtime history was merged from
[julia-script/silkjsruntime](https://github.com/julia-script/silkjsruntime); `git log --follow`
reaches commits from before the move.
