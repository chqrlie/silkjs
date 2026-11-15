'use strict';
// node:zlib must write the bytes of zlib 1.3.1: tests/fixtures/zlib/bytes.cjs over its matrix,
// against the digests a build with the C zlib 1.3.1 wrote (fixtures/zlib/zlib-1.3.1.digests.txt).
//
//   build/silkjs tests/zlib-bytes-smoke.cjs            # compare
//   <a build with the C zlib> tests/zlib-bytes-smoke.cjs --write   # renew the digests
//
// Node itself fails this: it builds Chromium's zlib, whose compressed bytes are others.
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const { collect } = require('./fixtures/zlib/bytes.cjs');

const file = path.join(__dirname, 'fixtures/zlib/zlib-1.3.1.digests.txt');
collect().then((lines) => {
  if (process.argv.includes('--write')) { fs.writeFileSync(file, lines.join('\n') + '\n'); console.log(`wrote ${lines.length} digests`); return; }
  const expected = fs.readFileSync(file, 'utf8').trimEnd().split('\n');
  let different = 0;
  for (let i = 0; i < Math.max(lines.length, expected.length); i++) {
    if (lines[i] !== expected[i]) { if (different++ < 10) console.error(`expected ${expected[i]}\n  actual ${lines[i]}`); }
  }
  // The byte that the digests leave out: the operating system of the gzip header.
  const system = zlib.gzipSync('x')[9];
  const wanted = process.platform === 'darwin' ? 19 : process.platform === 'win32' ? 10 : 3;
  if (system !== wanted) { console.error(`gzip header: operating system ${system}, zlib writes ${wanted} here`); different++; }
  if (different) { console.error(`${different} of ${expected.length} cases differ from zlib 1.3.1`); process.exitCode = 1; }
  else console.log(`ok: ${lines.length} cases have the bytes of zlib 1.3.1`);
});
