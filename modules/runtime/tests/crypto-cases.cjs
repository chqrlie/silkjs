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
// Keys: fixed keys (tests/fixtures/crypto/keys.json), so exports and deterministic signatures compare.
const keys = require('./fixtures/crypto/keys.json');
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
for (const [name, pem] of Object.entries(keys)) {
  record(`key ${name} details`, () => { const k = crypto.createPrivateKey(pem); return [k.type, k.asymmetricKeyType, k.asymmetricKeyDetails]; });
  for (const [type, format] of [['pkcs8', 'pem'], ['pkcs8', 'der']]) record(`key ${name} export ${type} ${format}`, () => sha(crypto.createPrivateKey(pem).export({ type, format })));
  record(`key ${name} public spki`, () => crypto.createPublicKey(pem).export({ type: 'spki', format: 'pem' }));
  record(`key ${name} public der`, () => sha(crypto.createPublicKey(pem).export({ type: 'spki', format: 'der' })));
  record(`key ${name} jwk`, () => crypto.createPrivateKey(pem).export({ format: 'jwk' }));
  record(`key ${name} public jwk`, () => crypto.createPublicKey(pem).export({ format: 'jwk' }));
  record(`key ${name} roundtrip der`, () => sha(crypto.createPrivateKey({ key: crypto.createPrivateKey(pem).export({ type: 'pkcs8', format: 'der' }), format: 'der', type: 'pkcs8' }).export({ type: 'pkcs8', format: 'der' })));
  record(`key ${name} encrypted pkcs8`, () => { const enc = crypto.createPrivateKey(pem).export({ type: 'pkcs8', format: 'pem', cipher: 'aes-256-cbc', passphrase: 'pw' }); return sha(crypto.createPrivateKey({ key: enc, passphrase: 'pw' }).export({ type: 'pkcs8', format: 'der' })); });
  record(`key ${name} wrong passphrase`, () => { const enc = crypto.createPrivateKey(pem).export({ type: 'pkcs8', format: 'pem', cipher: 'aes-128-cbc', passphrase: 'pw' }); return crypto.createPrivateKey({ key: enc, passphrase: 'nope' }); });
  record(`key ${name} missing passphrase`, () => { const enc = crypto.createPrivateKey(pem).export({ type: 'pkcs8', format: 'der', cipher: 'aes-128-cbc', passphrase: 'pw' }); return crypto.createPrivateKey({ key: enc, format: 'der', type: 'pkcs8' }); });
}
record('rsa pkcs1 export', () => [sha(crypto.createPrivateKey(keys.rsa).export({ type: 'pkcs1', format: 'pem' })), crypto.createPublicKey(keys.rsa).export({ type: 'pkcs1', format: 'pem' })]);
record('rsa pkcs1 import', () => sha(crypto.createPrivateKey(crypto.createPrivateKey(keys.rsa).export({ type: 'pkcs1', format: 'pem' })).export({ type: 'pkcs8', format: 'der' })));
record('rsa legacy encrypted', () => { const enc = crypto.createPrivateKey(keys.rsa).export({ type: 'pkcs1', format: 'pem', cipher: 'aes-128-cbc', passphrase: 'pw' }); return [enc.includes('DEK-Info: AES-128-CBC'), sha(crypto.createPrivateKey({ key: enc, passphrase: 'pw' }).export({ type: 'pkcs1', format: 'der' }))]; });
record('ec sec1 export', () => sha(crypto.createPrivateKey(keys['ec-prime256v1']).export({ type: 'sec1', format: 'der' })));
record('ec sec1 import', () => sha(crypto.createPrivateKey({ key: crypto.createPrivateKey(keys['ec-prime256v1']).export({ type: 'sec1', format: 'der' }), format: 'der', type: 'sec1' }).export({ type: 'pkcs8', format: 'der' })));
record('garbage key', () => crypto.createPrivateKey('-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n'));
record('public from private pem', () => crypto.createPublicKey(keys.ed25519).asymmetricKeyType);
for (const digest of ['sha1', 'sha256', 'sha384', 'sha512', 'sha3-256', 'md5']) {
  record(`rsa sign ${digest}`, () => crypto.sign(digest, Buffer.from('message'), keys.rsa).toString('base64'));
  record(`rsa verify ${digest}`, () => crypto.verify(digest, Buffer.from('message'), crypto.createPublicKey(keys.rsa), crypto.sign(digest, Buffer.from('message'), keys.rsa)));
}
record('rsa sign stream', () => crypto.createSign('SHA256').update('a').update('b').sign(keys.rsa, 'hex'));
record('rsa verify tampered', () => crypto.verify('sha256', Buffer.from('message!'), keys.rsa, crypto.sign('sha256', Buffer.from('message'), keys.rsa)));
for (const saltLength of [undefined, 0, 32]) record(`rsa pss ${saltLength}`, () => { const s = crypto.sign('sha256', Buffer.from('m'), { key: keys.rsa, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength }); return [s.length, crypto.verify('sha256', Buffer.from('m'), { key: keys.rsa, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength }, s)]; });
for (const curve of ['prime256v1', 'secp384r1', 'secp521r1', 'secp256k1']) for (const dsaEncoding of ['der', 'ieee-p1363']) record(`ecdsa ${curve} ${dsaEncoding}`, () => { const key = keys['ec-' + curve]; const s = crypto.sign('sha256', Buffer.from('m'), { key, dsaEncoding }); return [dsaEncoding === 'der' ? s[0] : s.length, crypto.verify('sha256', Buffer.from('m'), { key, dsaEncoding }, s), crypto.verify('sha256', Buffer.from('x'), { key, dsaEncoding }, s)]; });
record('ed25519 sign', () => { const s = crypto.sign(null, Buffer.from('m'), keys.ed25519); return [s.toString('hex'), crypto.verify(null, Buffer.from('m'), keys.ed25519, s), crypto.verify(null, Buffer.from('n'), keys.ed25519, s)]; });
record('x25519 derive', () => crypto.diffieHellman({ privateKey: crypto.createPrivateKey(keys.x25519), publicKey: crypto.createPublicKey(keys.x25519b) }).toString('hex'));
record('ecdh derive', () => crypto.diffieHellman({ privateKey: crypto.createPrivateKey(keys['ec-prime256v1']), publicKey: crypto.createPublicKey(keys['ec-prime256v1-b']) }).toString('hex'));
record('ecdh curve mismatch', () => crypto.diffieHellman({ privateKey: crypto.createPrivateKey(keys['ec-prime256v1']), publicKey: crypto.createPublicKey(keys['ec-secp384r1']) }));
for (const [padding, oaepHash] of [[crypto.constants.RSA_PKCS1_OAEP_PADDING, undefined], [crypto.constants.RSA_PKCS1_OAEP_PADDING, 'sha256'], [crypto.constants.RSA_NO_PADDING, undefined]]) record(`rsa crypt ${padding} ${oaepHash}`, () => { const data = padding === crypto.constants.RSA_NO_PADDING ? Buffer.alloc(256, 1) : Buffer.from('secret'); const enc = crypto.publicEncrypt({ key: keys.rsa, padding, oaepHash }, data); return [enc.length, crypto.privateDecrypt({ key: keys.rsa, padding, oaepHash }, enc).equals(data)]; });
record('rsa privateEncrypt', () => { const enc = crypto.privateEncrypt(keys.rsa, Buffer.from('hello')); return [enc.toString('base64'), crypto.publicDecrypt(keys.rsa, enc).toString()]; });
record('generate rsa', () => { const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 1024 }); return [publicKey.asymmetricKeyDetails, crypto.verify('sha256', Buffer.from('m'), publicKey, crypto.sign('sha256', Buffer.from('m'), privateKey))]; });
for (const type of ['ec', 'ed25519', 'x25519']) record(`generate ${type}`, () => { const { publicKey, privateKey } = crypto.generateKeyPairSync(type, { namedCurve: 'P-256' }); return [publicKey.asymmetricKeyType, privateKey.asymmetricKeyDetails]; });
record('generate pem encodings', () => { const { publicKey, privateKey } = crypto.generateKeyPairSync('ec', { namedCurve: 'secp384r1', publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'sec1', format: 'pem' } }); return [publicKey.split('\n')[0], privateKey.split('\n')[0]]; });
record('jwk import', () => { const jwk = crypto.createPrivateKey(keys['ec-prime256v1']).export({ format: 'jwk' }); return sha(crypto.createPrivateKey({ key: jwk, format: 'jwk' }).export({ type: 'pkcs8', format: 'der' })); });
record('getCurves has', () => ['prime256v1', 'secp384r1', 'secp521r1', 'secp256k1'].map(c2 => crypto.getCurves().includes(c2)));
record('ecdh object', () => { const a = crypto.createECDH('prime256v1'), b = crypto.createECDH('prime256v1'); a.setPrivateKey(Buffer.alloc(32, 7)); b.setPrivateKey(Buffer.alloc(32, 9)); return [a.getPublicKey('hex'), a.getPublicKey('hex', 'compressed'), a.computeSecret(b.getPublicKey()).toString('hex'), a.getPrivateKey('hex')]; });
record('ecdh generate', () => { const a = crypto.createECDH('secp384r1'); a.generateKeys(); const b = crypto.createECDH('secp384r1'); b.generateKeys(); return a.computeSecret(b.getPublicKey()).equals(b.computeSecret(a.getPublicKey())); });
record('ecdh convertKey', () => crypto.ECDH.convertKey(crypto.createECDH('prime256v1').setPrivateKey(Buffer.alloc(32, 3)) ?? crypto.createECDH('prime256v1'), 'prime256v1'));
record('ecdh bad curve', () => crypto.createECDH('nope'));
record('dh group', () => { const a = crypto.getDiffieHellman('modp14'), b = crypto.getDiffieHellman('modp14'); a.generateKeys(); b.generateKeys(); return [a.verifyError, a.getPrime('hex').length, a.getGenerator('hex'), a.computeSecret(b.getPublicKey()).equals(b.computeSecret(a.getPublicKey()))]; });
record('dh legacy group', () => { const a = crypto.getDiffieHellman('modp2'); a.setPrivateKey(Buffer.alloc(32, 5)); a.generateKeys(); return a.getPublicKey('hex'); });
record('dh custom', () => { const prime = crypto.getDiffieHellman('modp5').getPrime(); const a = crypto.createDiffieHellman(prime, Buffer.from([2])); return [a.verifyError, a.getPrime().equals(prime)]; });
for (const type of ['ed448', 'x448']) record(`generate ${type}`, () => { const { publicKey, privateKey } = crypto.generateKeyPairSync(type); return [publicKey.asymmetricKeyType, privateKey.export({ format: 'der', type: 'pkcs8' }).length, publicKey.export({ format: 'der', type: 'spki' }).length]; });
record('ed448 sign', () => { const { publicKey, privateKey } = crypto.generateKeyPairSync('ed448'); const s = crypto.sign(null, Buffer.from('m'), privateKey); return [s.length, crypto.verify(null, Buffer.from('m'), publicKey, s)]; });
record('x448 derive', () => { const a = crypto.generateKeyPairSync('x448'), b = crypto.generateKeyPairSync('x448'); return crypto.diffieHellman({ privateKey: a.privateKey, publicKey: b.publicKey }).equals(crypto.diffieHellman({ privateKey: b.privateKey, publicKey: a.publicKey })); });
for (const name of ['des-ede3-cbc', 'des-ede3']) record(`cipher ${name}`, () => { const c = crypto.createCipheriv(name, key(24), name === 'des-ede3' ? null : iv(8)); const enc = Buffer.concat([c.update(big), c.final()]); const d = crypto.createDecipheriv(name, key(24), name === 'des-ede3' ? null : iv(8)); return [sha(enc), Buffer.concat([d.update(enc), d.final()]).equals(big)]; });
for (const name of ['id-aes128-wrap', 'aes256-wrap', 'id-aes192-wrap-pad']) record(`cipher ${name}`, () => { const k = key(name.includes('128') ? 16 : name.includes('192') ? 24 : 32); const ivv = name.endsWith('pad') ? Buffer.from('a65959a6', 'hex') : Buffer.from('a6a6a6a6a6a6a6a6', 'hex'); const data = Buffer.alloc(name.endsWith('pad') ? 20 : 32, 0x11); const c = crypto.createCipheriv(name, k, ivv); const enc = Buffer.concat([c.update(data), c.final()]); const d = crypto.createDecipheriv(name, k, ivv); return [enc.toString('hex'), Buffer.concat([d.update(enc), d.final()]).equals(data)]; });
record('ccm', () => { const c = crypto.createCipheriv('aes-128-ccm', key(16), iv(12), { authTagLength: 16 }); c.setAAD(Buffer.from('aad'), { plaintextLength: 5 }); const enc = Buffer.concat([c.update('hello'), c.final()]); const tag = c.getAuthTag(); const d = crypto.createDecipheriv('aes-128-ccm', key(16), iv(12), { authTagLength: 16 }); d.setAuthTag(tag); d.setAAD(Buffer.from('aad'), { plaintextLength: 5 }); return [enc.toString('hex'), tag.toString('hex'), Buffer.concat([d.update(enc), d.final()]).toString()]; });
let pending = 0;
function later(name, start) { pending++; start((e, v) => { out.push(e ? { name, error: { code: e.code, message: e.message } } : { name, value: v }); if (--pending === 0) finish(); }); }
later('pbkdf2 async', cb => crypto.pbkdf2('p', 's', 10, 16, 'sha256', (e, k) => cb(e, k && k.toString('hex'))));
later('scrypt async', cb => crypto.scrypt('p', 's', 16, (e, k) => cb(e, k && k.toString('hex'))));
later('hkdf async', cb => crypto.hkdf('sha256', 'k', 's', 'i', 16, (e, k) => cb(e, k && Buffer.from(k).toString('hex'))));
later('randomBytes async', cb => crypto.randomBytes(16, (e, b) => cb(e, b && b.length)));
later('keygen async', cb => crypto.generateKeyPair('ed25519', (e, pub, priv) => cb(e, pub && [pub.asymmetricKeyType, priv.type])));
later('subtle ecdsa', cb => crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify']).then(async k => { const s = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, k.privateKey, Buffer.from('m')); return [s.byteLength, await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, k.publicKey, s, Buffer.from('m'))]; }).then(v => cb(null, v), cb));
later('subtle digest', cb => crypto.subtle.digest('SHA-384', Buffer.from('abc')).then(d => cb(null, Buffer.from(d).toString('hex')), cb));
function finish() { for (const line of out) console.log(JSON.stringify(line, (k, v) => typeof v === "bigint" ? `${v}n` : v)); }
