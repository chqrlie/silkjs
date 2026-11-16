// Prints one JSON line per case: what a runtime's crypto computes for hashes, HMAC, key
// derivation and the symmetric ciphers, and how each fails, for tests/crypto-differential.mjs to
// compare with Node's.
const crypto = require('crypto');
const out = [];
function record(name, fn) {
  try { out.push({ name, value: fn() }); }
  catch (e) { out.push({ name, error: { code: e.code, message: e.message, name: e.name } }); }
}
const big = Buffer.alloc(100003); for (let i = 0; i < big.length; i++) big[i] = (i * 31 + 7) & 255;
const inputs = { empty: Buffer.alloc(0), abc: Buffer.from('abc'), big };
const hashes = ['md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512', 'sha512-224', 'sha512-256', 'sha3-224', 'sha3-256', 'sha3-384',
  'sha3-512', 'shake128', 'shake256', 'blake2b512', 'blake2s256', 'ripemd160', 'sm3', 'md5-sha1', 'SHA256', 'RSA-SHA256', 'sha-256', 'dss1', 'rmd160'];
for (const h of hashes) for (const [label, input] of Object.entries(inputs)) record(`hash ${h} ${label}`, () => crypto.createHash(h).update(input).digest('hex'));
for (const h of hashes) record(`hash ${h} split`, () => { const x = crypto.createHash(h); for (let i = 0; i < big.length; i += 9973) x.update(big.subarray(i, i + 9973)); return x.digest('base64'); });
for (const h of hashes) record(`hash one-shot ${h}`, () => crypto.hash(h, 'one shot', 'hex'));
record('hash copy', () => { const a = crypto.createHash('sha256').update('one'); const b = a.copy(); b.update('two'); return [a.digest('hex'), b.digest('hex')]; });
record('shake128 length', () => crypto.createHash('shake128', { outputLength: 100 }).update('x').digest('hex'));
record('shake256 length 0', () => crypto.createHash('shake256', { outputLength: 0 }).update('x').digest('hex'));
record('sha256 outputLength', () => crypto.createHash('sha256', { outputLength: 16 }).update('x').digest('hex'));
record('unknown hash', () => crypto.createHash('nope'));
record('hash digest twice', () => { const x = crypto.createHash('md5'); x.digest(); return x.digest(); });
for (const h of hashes.filter(h => !h.startsWith('shake'))) for (const key of ['', 'key', 'k'.repeat(200)]) record(`hmac ${h} key ${key.length}`, () => crypto.createHmac(h, key).update('message').update(big).digest('hex'));
record('hmac shake', () => crypto.createHmac('shake128', 'k').update('x').digest('hex'));
record('hmac digest twice', () => { const x = crypto.createHmac('sha1', 'k'); x.digest(); return x.digest('hex'); });
for (const h of ['sha1', 'sha256', 'sha512', 'md5', 'sha3-256']) for (const [i, len] of [[1, 20], [1000, 64], [2, 100]]) record(`pbkdf2 ${h} ${i} ${len}`, () => crypto.pbkdf2Sync('password', 'salt', i, len, h).toString('hex'));
record('pbkdf2 bad digest', () => crypto.pbkdf2Sync('p', 's', 1, 10, 'nope'));
for (const [N, r, p, len] of [[16, 1, 1, 64], [1024, 8, 1, 32], [256, 2, 3, 48]]) record(`scrypt ${N} ${r} ${p} ${len}`, () => crypto.scryptSync('password', 'NaCl', len, { N, r, p }).toString('hex'));
record('scrypt maxmem', () => crypto.scryptSync('p', 's', 16, { N: 1 << 20, maxmem: 1024 }));
for (const h of ['sha1', 'sha256', 'sha512', 'md5']) for (const len of [1, 42, 255]) record(`hkdf ${h} ${len}`, () => Buffer.from(crypto.hkdfSync(h, 'key', 'salt', 'info', len)).toString('hex'));
record('hkdf too long', () => crypto.hkdfSync('sha256', 'k', 's', 'i', 255 * 32 + 1));
const key = n => Buffer.alloc(n, 0x42), iv = n => Buffer.alloc(n, 0x24);
const plain = { short: Buffer.from('hello'), block: Buffer.alloc(32, 1), big };
for (const bits of [128, 192, 256]) for (const mode of ['ecb', 'cbc', 'cfb', 'cfb8', 'cfb1', 'ofb', 'ctr']) for (const [label, input] of Object.entries(plain)) {
  const name = `aes-${bits}-${mode}`, ivLength = mode === 'ecb' ? 0 : 16;
  record(`cipher ${name} ${label}`, () => {
    const c = crypto.createCipheriv(name, key(bits / 8), ivLength ? iv(ivLength) : null);
    const enc = Buffer.concat([c.update(input), c.final()]);
    const d = crypto.createDecipheriv(name, key(bits / 8), ivLength ? iv(ivLength) : null);
    const dec = Buffer.concat([d.update(enc), d.final()]);
    return [crypto.createHash('sha256').update(enc).digest('hex'), dec.equals(input)];
  });
}
for (const bits of [128, 256]) for (const ivLength of [12, 16, 1]) for (const tagLength of [16, 12, 4]) record(`gcm ${bits} iv ${ivLength} tag ${tagLength}`, () => {
  const name = `aes-${bits}-gcm`;
  const c = crypto.createCipheriv(name, key(bits / 8), iv(ivLength), { authTagLength: tagLength });
  c.setAAD(Buffer.from('header'));
  const enc = Buffer.concat([c.update(big), c.final()]); const tag = c.getAuthTag();
  const d = crypto.createDecipheriv(name, key(bits / 8), iv(ivLength), { authTagLength: tagLength });
  d.setAAD(Buffer.from('header')); d.setAuthTag(tag);
  const dec = Buffer.concat([d.update(enc), d.final()]);
  return [crypto.createHash('sha256').update(enc).digest('hex'), tag.toString('hex'), dec.equals(big)];
});
record('gcm bad tag', () => {
  const c = crypto.createCipheriv('aes-128-gcm', key(16), iv(12)); const enc = Buffer.concat([c.update('data'), c.final()]);
  const d = crypto.createDecipheriv('aes-128-gcm', key(16), iv(12)); d.setAuthTag(Buffer.alloc(16)); d.update(enc); return d.final();
});
record('chacha20-poly1305', () => {
  const c = crypto.createCipheriv('chacha20-poly1305', key(32), iv(12), { authTagLength: 16 }); c.setAAD(Buffer.from('aad'));
  const enc = Buffer.concat([c.update(big), c.final()]); const tag = c.getAuthTag();
  const d = crypto.createDecipheriv('chacha20-poly1305', key(32), iv(12), { authTagLength: 16 }); d.setAAD(Buffer.from('aad')); d.setAuthTag(tag);
  return [enc.subarray(0, 64).toString('hex'), tag.toString('hex'), Buffer.concat([d.update(enc), d.final()]).equals(big)];
});
record('chacha20', () => { const c = crypto.createCipheriv('chacha20', key(32), iv(16)); return Buffer.concat([c.update('stream cipher text'), c.final()]).toString('hex'); });
record('cbc no padding', () => { const c = crypto.createCipheriv('aes-128-cbc', key(16), iv(16)); c.setAutoPadding(false); return Buffer.concat([c.update(Buffer.alloc(32, 3)), c.final()]).toString('hex'); });
record('cbc no padding partial', () => { const c = crypto.createCipheriv('aes-128-cbc', key(16), iv(16)); c.setAutoPadding(false); c.update(Buffer.alloc(10)); return c.final(); });
record('cbc bad decrypt', () => { const d = crypto.createDecipheriv('aes-128-cbc', key(16), iv(16)); d.update(Buffer.alloc(16, 9)); return d.final(); });
record('cbc wrong key length', () => crypto.createCipheriv('aes-128-cbc', key(15), iv(16)));
record('cbc wrong iv length', () => crypto.createCipheriv('aes-128-cbc', key(16), iv(15)));
record('unknown cipher', () => crypto.createCipheriv('aes-128-xyz', key(16), iv(16)));
for (const name of ['aes-128-cbc', 'aes-256-gcm', 'id-aes128-gcm', 'aes128', 'chacha20-poly1305', 'chacha20', 'aes-192-ctr', 'aes-128-cfb8', 'nope']) record(`cipherInfo ${name}`, () => crypto.getCipherInfo(name) ?? null);
record('cipherInfo nid', () => crypto.getCipherInfo(419));
record('cipherInfo keyLength', () => [crypto.getCipherInfo('aes-128-cbc', { keyLength: 16 }), crypto.getCipherInfo('aes-128-cbc', { keyLength: 17 }) ?? null]);
record('random length', () => [crypto.randomBytes(0).length, crypto.randomBytes(33).length, crypto.randomUUID().length, crypto.getRandomValues(new Uint8Array(8)).length]);
record('timingSafeEqual', () => [crypto.timingSafeEqual(Buffer.from('ab'), Buffer.from('ab')), crypto.timingSafeEqual(Buffer.from('ab'), Buffer.from('ac'))]);
record('getHashes has', () => ['sha256', 'md5', 'sha3-512', 'shake256', 'RSA-SHA256'].map(h => crypto.getHashes().includes(h)));
record('getCiphers has', () => ['aes-128-cbc', 'aes-256-gcm', 'chacha20-poly1305'].map(c => crypto.getCiphers().includes(c)));
let pending = 0;
function later(name, start) { pending++; start((e, v) => { out.push(e ? { name, error: { code: e.code, message: e.message } } : { name, value: v }); if (--pending === 0) finish(); }); }
later('pbkdf2 async', cb => crypto.pbkdf2('p', 's', 10, 16, 'sha256', (e, k) => cb(e, k && k.toString('hex'))));
later('scrypt async', cb => crypto.scrypt('p', 's', 16, (e, k) => cb(e, k && k.toString('hex'))));
later('hkdf async', cb => crypto.hkdf('sha256', 'k', 's', 'i', 16, (e, k) => cb(e, k && Buffer.from(k).toString('hex'))));
later('randomBytes async', cb => crypto.randomBytes(16, (e, b) => cb(e, b && b.length)));
later('subtle digest', cb => crypto.subtle.digest('SHA-384', Buffer.from('abc')).then(d => cb(null, Buffer.from(d).toString('hex')), cb));
function finish() { for (const line of out) console.log(JSON.stringify(line)); }
