# zlib in Silk

`node:zlib` (deflate, inflate, raw deflate, gzip, gunzip, unzip, `crc32`) runs on Silk code. No
zlib C is compiled or linked: `modules/native/zlib` is gone, and `nm build/silkjs` has no zlib
symbol. Zstandard and Brotli are as they were.

| Layer | File | What it holds |
| --- | --- | --- |
| Codecs | `src/stdlib_copy/compress/deflate.silk`, `inflate.silk` | Local copies of the standard library's `silk.compress.deflate` and `silk.compress.inflate` (Silk 0.1.0-dev.74+205aea3f5b2e) with the changes listed below. Nothing in them is specific to Node or SilkJS. |
| Adapter | `src/ext/zlib_stream.silk` | zlib's calling convention over the codecs: window bits that choose a format, gzip or zlib by the first two bytes, zlib's status codes and message texts, the gzip operating system byte. |
| Binding | `src/ext/compression_native.silk` | As before, with the zlib kinds calling the adapter where they called C. |

**The local copies are temporary.** The changes are upstream in
[silk-lang/silk#3122](https://github.com/silk-lang/silk/pull/3122). Once a Silk release contains
them: move `silk-version` to that release, delete `src/stdlib_copy`, point the imports in
`src/ext/zlib_stream.silk` (and `tests/zlib-differential/build.sh`) back at
`silk.compress.deflate` and `silk.compress.inflate`, and run `tests/zlib-differential` and
`tests/zlib-bytes-smoke.cjs` again.

## Who else used zlib

Checked at main `df9c30b`. Only the runtime's own binding linked against it.

| Possible consumer | Finding |
| --- | --- |
| `src/ext/compression_native.silk` | The only caller: `deflateInit2_`, `deflate`, `deflateParams`, `deflateReset`, `deflateSetDictionary`, `deflateEnd`, `inflateInit2_`, `inflate`, `inflateReset`, `inflateSetDictionary`, `inflateEnd`. |
| `src/ext/os_native.silk` | `zlibVersion`, for `process.versions.zlib`. It still says `1.3.1`, the zlib whose bytes and behaviour the Silk code reproduces. |
| `zlib.crc32` | Already `silk.compress.checksum` before this change. |
| OpenSSL (`modules/native/openssl`) | Built with `OPENSSL_NO_ZLIB` (`generated/include/openssl/configuration.h`); `crypto/comp/c_zlib.c` compiles to stubs. |
| nghttp2, libuv, Zstandard, Brotli | No reference. |
| WAMR (`vendor/wamr`) | No reference. |
| SQLite (`db-sqlite` of lib-sqlite) | No zlib option in its manifest (no `zipfile`, `sqlar` or `SQLITE_HAVE_ZLIB`). |
| Package manager (`modules/pm`) | Unpacks tarballs with `silk.compress.inflate` of the standard library (`pkg/store.silk`), never the C zlib. It does not use the local copies. |
| Engine (`modules/engine`) | `intl_zone_name.silk` uses `silk.compress.inflate` of the standard library. |
| fetch/undici content decoding, WebSocket permessage-deflate, `CompressionStream` | JavaScript over `node:zlib`, so they follow the binding. |

The link is the proof: the executable links with the package removed.

## What the standard library already had

`silk.compress.deflate` (3,551 lines) and `silk.compress.inflate` (2,088 lines) of the pinned
release, plus `silk.compress.checksum` (CRC-32, Adler-32).

- **Deflate:** an encoder modelled on zlib: levels 0 to 9, the five strategies, window bits 8 to
  15, memory levels 1 to 9, all six flush modes, preset dictionaries, `reset`, a change of
  parameters, raw, zlib and gzip framing, steps with any input and output lengths. Its Huffman
  tree code (`trees.c`) already produced zlib's blocks. Its own documentation said: "The bytes are
  a valid stream but are not the same bytes that zlib writes."
- **Inflate:** a decoder of its own design (64-bit bit buffer, a fast loop with unaligned 8 and 16
  byte copies and a BMI2 variant), raw, zlib and gzip with several members, preset dictionaries,
  limits, steps with any input and output lengths. One coarse error kind per class of failure.
- **Missing for Node:** zlib's bytes; zlib's exact accounting of consumed and written bytes per
  call; zlib's error causes (the message texts); window bits for inflate; stopping at the end of
  one gzip member; `Z_BLOCK` for inflate; the gzip operating system byte.

## Changes to the copied files

Each is generic: it makes the codec match zlib 1.3.1, or adds an option that zlib has.

### `deflate.silk`

Found with a differential fuzzer against zlib 1.3.1 (below). Before these changes the first
mismatch came within a few scenarios; after them none in the runs recorded under "Verification".

| # | Change | Why |
| --- | --- | --- |
| D1 | `step` follows zlib's `deflate()` call for call: zlib's `last_flush` (with "never" and "output was full"), the rank test for a repeated flush, the header and trailer flushes, and a `FINISHING` phase for a last block that is written before the trailer. | The encoder wrote the trailer and counted flushes its own way, so consumed and written bytes per call, and sometimes the bytes, differed. As in zlib, a `Sync` or `Full` flush whose output fills can now write its marker again (zlib documents this; give more than 6 bytes of room). |
| D2 | Level 0 is zlib's `deflate_stored`: stored blocks go straight to the output, and their lengths depend on the room there. | The old level 0 cut 32 KiB blocks through the window; every level 0 stream differed from zlib's. |
| D3 | `flushBlock` reports "the output has room" as zlib's `FLUSH_BLOCK` tests it, and lazy matching returns after a literal when the output is full. | The encoder consumed more input in a call than zlib does when a block exactly filled the output. |
| D4 | `setDictionary` is zlib's `deflateSetDictionary`: it goes through the window fill, hashes every position, takes the tail of a long dictionary, adds to the dictionary id, and is allowed mid-stream for raw. The zlib header sets the dictionary flag when the window holds bytes, not when the method was called. | Different hash chains gave different matches; an empty dictionary set the flag. |
| D5 | `setParameters` follows `deflateParams`: the block is ended only when the strategy or the kind of level (0, 1 to 3, 4 to 9) changes and a step ran before; the hash is slid or cleared when leaving level 0. | The encoder ended a block on every change, and left stale hash chains after level 0. |
| D6 | The window slide moves only the bytes up to the end of the data, as zlib's `fill_window`. | The longest match can read past the data; what stays there must be what zlib leaves, or matches differ at the end of the input after a slide. |
| D7 | `longestMatch` starts from the previous length as it is, with no lower bound of 2. | After a change from lazy to greedy matching the previous length can be 0, and zlib then also compares the byte before the candidate. |
| D8 | The RLE strategy clears the match length on each pass. | A later change of strategy starts from it (zlib sets `match_length = 0`). |
| D9 | `Encoder.setGzipSystem(code)`: the operating system byte of the gzip header, 255 by default as before. | zlib writes its build's system (3 on Unix, 19 on macOS); the adapter sets it. |
| D10 | `reset` keeps the match start and sets the dictionary id to 1 (Adler-32 of nothing). | As `deflateReset`. |
| D11 | The match search reads through pointers after proving once that its reads are in the window, and compares 8 bytes at a time; the hash insert is inlined and goes through one slice per table. | Speed. After D1–D10 the encoder ran at 0.80–0.95 of zlib's throughput; callgrind, 2 MiB of text at level 6: 948M instructions, zlib 489M. After: about 580M, and 1.04 of zlib's throughput on average. A vectorized hash slide (`u16x8.saturatingSubtract`) was tried and measured no better, so it was not kept. |

### `inflate.silk`

| # | Change | Why |
| --- | --- | --- |
| I1 | `ErrorDetail` and `Decoder.failureDetail()`: the cause of a failure with zlib's granularity (18 causes; `ErrorKind` is unchanged). | Node reports zlib's message ("invalid stored block lengths", "invalid distance too far back", ...). One kind covered several. |
| I2 | Headers are checked in zlib's units and order: the two zlib header bytes together (check, method, window), the gzip magic as two bytes, method with flags; a dictionary id is read before its absence is reported. | With one byte of input the decoder failed where zlib waits for the second; the cause differed for some headers. |
| I3 | `Decoder.setWindowBits(bits)`, as `inflateInit2`: a match reaches `1 << bits` bytes of earlier output plus all output of its own step; a zlib header naming a larger window fails; 0 takes the header's. Unset, the old strict behaviour stays. | Node passes `windowBits` to inflate; "invalid window size" and "invalid distance too far back" depend on it. |
| I4 | `Decoder.setMemberStop(true)`: a gzip decoder finishes at the end of the first member and leaves the rest unconsumed. | zlib returns `Z_STREAM_END` there; Node decides about the next member itself. Before, the decoder waited for more input or the final input. |
| I5 | `Decoder.setBlockStop(true)`: a step returns at the start of each block. | `Z_BLOCK` for inflate. |
| I6 | A literal that meets a full output is consumed and held (`PHASE_LITERAL`); the distance of a match is checked when the copy has room, and again when a later step continues it. | zlib decodes before it looks for room, so the input consumed when the output fills is the same now; and an invalid distance is reported in the same call as zlib reports it. |
| I7 | A dynamic block with more than 30 distance codes fails (`SymbolCounts`); a repeat is checked after its extra bits are there; a code-length code with no lengths at all reads as zlib reads it. | Same accept, reject and "need more input" as zlib on damaged data. |
| I8 | A final input that ends inside a header unit whose first bytes already prove it wrong fails with that cause, not `TruncatedInput`; a zlib header with both a bad check and a bad method keeps the kind `UnsupportedMethod` (the detail is zlib's `HeaderCheck`). | The standard library's own corpus (`streaming-inflate`, `http-content-decoding`) relies on those kinds; with I8 it passes unchanged. |

## Compressed bytes

- Against zlib 1.3.1: **equal.** `tests/zlib-bytes-smoke.cjs` compares 1,089 cases (levels,
  strategies, window and memory levels, dictionaries, chunk sizes, flushes, parameter changes,
  resets, with the lengths of the emitted chunks) with digests written by a build that linked the C
  zlib 1.3.1.
- Against Node 24: Node builds Chromium's zlib (`process.versions.zlib` is
  `1.3.2.1-motley-285e94b`), which hashes differently. 697 of the 1,089 cases differ between Node
  24.22.3 and zlib 1.3.1, so the runtime with the C zlib never had Node's bytes either. Matching
  Node's bytes would mean porting Chromium's deflate changes, not zlib's.

## Verification

Last run on the final code, branch `feature/zlib-silk`, Linux x86_64:

- Differential fuzzers (`tests/zlib-differential`) against zlib 1.3.1, comparing status, message,
  consumed, written and bytes per call: deflate 90,000 scenarios, inflate 180,000 scenarios over
  the course of the work; the last pass on the final code was 10,000 deflate and 30,000 inflate on
  fresh seeds, 0 mismatches.
- `tests/zlib-bytes-smoke.cjs`: 1,089 of 1,089 cases have the bytes of zlib 1.3.1.
- `silk task compat` against Node 24.22.3: 79 passed, 0 failed (75 existing cases, and 4 new zlib
  ones: the behaviour matrix and three fuzz seeds). The baseline with the C zlib passes the same.
- `tests/ws-native-smoke.cjs` and `tests/web-boundaries-smoke.cjs`: pass on both builds.
- `nm build/silkjs`: no zlib symbol.

## Performance

Both builds `silk task build --release` of this branch's base and head, run in turns. Host noise
is about 8%.

Codec, `tests/zlib-differential/bench_codec`, five rounds, 4 to 16 MiB inputs (text, JSON, random,
an executable), levels 1, 6 and 9, gzip and raw, one call and 1 KiB calls; throughput of the Silk
code over zlib 1.3.1 at `-O2`:

| | mean | min | max |
| --- | --- | --- | --- |
| deflate | 1.04 | 0.89 | 1.26 |
| inflate | 2.65 | 0.81 | 5.05 |

Through `node:zlib` (`tools/zlib_bench.py`, three rounds, 96 cases plus crc32): the codec is a
small part of the time there. The engine copies the input into the native side and the output
back byte by byte, so most cases run at 4 to 60 MB/s on both builds. Medians, after over before:

| Group | Ratio range | Typical |
| --- | --- | --- |
| deflate, one call | 0.83 – 1.04 | 0.91 |
| deflate, 1 KiB writes | 0.76 – 1.29 | 1.04 |
| inflate, one call | 0.78 – 1.05 | 0.90 |
| inflate, 1 KiB writes | 0.78 – 1.35 | 1.10 |
| crc32 (same Silk code in both) | 0.80 – 1.21 | 0.92 |
| HTTP server gzip + fetch gunzip, requests/s | 1.00 | |
| peak RSS, 8 single cases and the HTTP case | 0.96 – 1.07 | +4 MB |

The one-call cases look about 10% slower even though the codec itself is faster. crc32 runs the
same Silk code in both builds and still varies from 0.80 to 1.21, so differences of this size are
within the noise of these runs. The extra 4 MB of RSS is in every case, idle ones too. It is the
size of the executable's code (the binary grew 29 KB), not codec memory: the Silk encoder
allocates the same tables as zlib's at the same memory level, and the decoder 64 KiB, against
zlib's 7 KiB of state plus a 32 KiB window.

Not measured: a package install through `modules/pm`. The package manager unpacks with the
standard library's own `silk.compress.inflate`, which this change does not touch.

## Compiler walls

None. The encoder needed unaligned 8-byte loads, a trailing-zero count, and `u16x8` saturating
subtraction (tried, not kept). The standard library has all three. The decoder already had SIMD
copies and a BMI2 build.

## Upstreaming

[silk-lang/silk#3122](https://github.com/silk-lang/silk/pull/3122), a draft: the two files as in
`src/stdlib_copy` (without their header note), `corpus/native/deflate-zlib-bytes`, a changeset with
migration notes, and the regenerated `reference/stdlib`. Before it was opened, these ran with
Silk 0.1.0-dev.74: nine `corpus/native` programs, `silk doctest --stdlib` (414 passed, 0 failed),
and `doc-reference --check`. Once a release contains it, delete `src/stdlib_copy` as described at
the top of this file.
