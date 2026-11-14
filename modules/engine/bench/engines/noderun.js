globalThis.print = console.log.bind(console);
require('vm').runInThisContext(require('fs').readFileSync(process.argv[2], 'utf8'), {filename: process.argv[2]});
