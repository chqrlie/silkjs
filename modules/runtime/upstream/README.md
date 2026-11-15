# For silk-lang/silk

`0001-silk-compress-zlib-exact.patch` is the change to the standard library that
`src/stdlib_copy` carries (see `../ZLIB_NOTES.md`): `git am` it on silk-lang/silk `main`
(made on `8cc0e2d`, where `stdlib/silk/compress/deflate.silk` and `inflate.silk` are still those
of Silk 0.1.0-dev.74).

It is a patch here, not a pull request, because this session was refused push access to
silk-lang/silk. Once it is a pull request, link it in `ZLIB_NOTES.md` and delete this folder.

What it holds:

- `stdlib/silk/compress/deflate.silk`, `inflate.silk`: the copies of `src/stdlib_copy/compress`
  without their note, with deflate importing `silk.compress.inflate` again.
- `corpus/native/deflate-zlib-bytes`: bytes that zlib 1.3.1 wrote for seven option sets, through
  16 ways to cut the input and output into steps; the gzip system byte; thirteen failure causes;
  window bits; the member stop; the block stop.
- `.changes/7c1e5b0a9d3f4e62a8b4c0d1e2f3a4b5.md`: the changeset, with its migration notes.

Checked, with the Silk 0.1.0-dev.74 compiler and that release's standard library with the two
files replaced: `deflate-zlib-bytes`, `deflate-encode`, `streaming-inflate`, `inflate-fast-loop`,
`tar-write`, `http-content-decoding`, `http-exchange-content`, `http-exchange-request` and
`format-debug-errors` of `corpus/native` end with their expected exit codes (the existing eight
are unchanged; three of them needed `--ignore-warnings` for warnings they already have with
this compiler).

Not done, and needed before it merges:

- `reference/stdlib` regenerated with `doc-reference` (it needs the repository's own compiler).
- The repository's CI (`Focused Linux Silk`, Selfhost); only the nine corpus programs above ran.
- A look at `integration/inflate-benchmark` for the decoder (the codec benchmark of
  `tests/zlib-differential` shows no slowdown; that program was not run).
