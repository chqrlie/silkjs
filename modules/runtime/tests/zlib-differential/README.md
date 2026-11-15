# zlib differential harness

The runtime's zlib is Silk code (`src/ext/zlib_stream.silk` over `src/stdlib_copy/compress`). These
programs link it next to zlib 1.3.1, built from the pinned release, and give both the same calls.

| Program | What it compares |
| --- | --- |
| `fuzz_deflate <first seed> <count>` | Per `deflate` call: status, input consumed, output written, and the bytes. Scenarios vary the level, strategy, window and memory level, the format, dictionaries, the input and output lengths of each call, every flush mode, `deflateParams` as Node calls it, and `deflateReset`. |
| `fuzz_inflate <first seed> <count>` | Per `inflate` call: status, zlib's message, consumed, written, bytes. Streams are valid, damaged (bits flipped, cut, bytes replaced, noise), with several gzip members or bytes after the end, opened with the right or another format and window, with the right, a wrong or no dictionary, with `Z_BLOCK` and `Z_FINISH`. |
| `bench_codec <large file> <rounds>` | Throughput of both, in turns, for the cases of `bench/scripts/zlib.cjs`. |

`./build.sh` writes them to `build/zlib-differential/`. A mismatch prints its seed; a third
argument to a fuzzer traces that seed's calls. `ZLIB_NOTES.md` has the results.
