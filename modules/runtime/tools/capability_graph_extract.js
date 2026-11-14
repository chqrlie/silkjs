// Reads built-in JavaScript files and prints, for each, what tools/capability_graph.py needs:
// every require and import string (load-time or lazy) and every name it reads from `host`.
// Runs under Node (or any engine with CommonJS) with the vendored acorn; nothing is executed.
//
//   node tools/capability_graph_extract.js < list.json > facts.json
//
// The input is a JSON array of { name, path }. Built-ins are function bodies (tools/embed.py
// wraps each in a factory), so each is parsed as one, and "load-time" means outside any nested
// function: it runs when the built-in is first required.
'use strict';
const fs = require('fs');
const path = require('path');
const acorn = require(path.join(__dirname, '../ext/node/internal/deps/acorn/acorn/dist/acorn.js'));

const PREFIX = '(function(module, exports, require, process) {\n';
const SUFFIX = '\n})';
const FUNCTIONS = new Set(['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression']);

function isHost(node) {
  // `host`, `x.host`, `x.y.host`.
  if (!node) return false;
  if (node.type === 'Identifier') return node.name === 'host';
  if (node.type === 'MemberExpression' && !node.computed) return node.property.name === 'host';
  return false;
}

function stringOf(node) {
  if (!node) return undefined;
  if (node.type === 'Literal' && typeof node.value === 'string') return node.value;
  if (node.type === 'TemplateLiteral' && node.expressions.length === 0) return node.quasis[0].value.cooked;
  return undefined;
}

function facts(source, wrapped) {
  const ast = acorn.parse(wrapped ? PREFIX + source + SUFFIX : source, {
    ecmaVersion: 'latest', sourceType: wrapped ? 'script' : 'module',
    allowReturnOutsideFunction: true, allowHashBang: true, allowAwaitOutsideFunction: true,
  });
  const requires = new Map(); // specifier -> 'load' | 'lazy'
  const dynamic = { load: 0, lazy: 0 };
  const natives = new Set();
  const note = (specifier, depth) => {
    const when = depth > 0 ? 'lazy' : 'load';
    if (requires.get(specifier) !== 'load') requires.set(specifier, when);
  };
  // The factory itself is depth 0 when wrapped; a module's top level is depth 0 otherwise.
  const base = wrapped ? 1 : 0;
  (function visit(node, depth) {
    if (!node || typeof node.type !== 'string') return;
    if (FUNCTIONS.has(node.type)) depth++;
    switch (node.type) {
      case 'CallExpression': {
        const callee = node.callee;
        if (callee.type === 'Identifier' && (callee.name === 'require' || callee.name === 'internalRequire')) {
          const specifier = stringOf(node.arguments[0]);
          if (specifier !== undefined) note(specifier, depth - base);
          else dynamic[depth - base > 0 ? 'lazy' : 'load']++;
        }
        break;
      }
      case 'ImportExpression': {
        const specifier = stringOf(node.source);
        if (specifier !== undefined) note(specifier, 1);
        break;
      }
      case 'ImportDeclaration': case 'ExportAllDeclaration': case 'ExportNamedDeclaration':
        if (node.source) note(node.source.value, 0);
        break;
      case 'MemberExpression':
        if (!node.computed && isHost(node.object)) natives.add(node.property.name);
        if (node.computed && isHost(node.object) && stringOf(node.property)) natives.add(stringOf(node.property));
        break;
      case 'VariableDeclarator':
        if (node.id.type === 'ObjectPattern' && isHost(node.init)) {
          for (const property of node.id.properties) {
            if (property.type === 'Property' && !property.computed && property.key.type === 'Identifier') natives.add(property.key.name);
          }
        }
        break;
    }
    for (const key of Object.keys(node)) {
      if (key === 'type' || key === 'start' || key === 'end' || key === 'loc') continue;
      const child = node[key];
      if (Array.isArray(child)) for (const item of child) visit(item, depth);
      else if (child && typeof child.type === 'string') visit(child, depth);
    }
  })(ast, 0);
  return { requires: Object.fromEntries([...requires].sort()), dynamic, natives: [...natives].sort() };
}

const list = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const { name, path: file, module } of list) {
  const source = fs.readFileSync(file, 'utf8');
  try {
    out[name] = facts(source, !module);
  } catch (error) {
    out[name] = { error: String(error.message), requires: {}, dynamic: { load: 0, lazy: 0 }, natives: [] };
  }
}
process.stdout.write(JSON.stringify(out));
