// Development-only runner. No packages or browser installation required.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '..', 'dashboard');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1, 'Expected exactly one inline dashboard script');
const startup = '\nrefresh().then(history);\nrefreshSystem();\nsetInterval(freshness, 1000);';
assert(scripts[0][1].trimEnd().endsWith(startup), 'Dashboard startup changed; update the test harness');
const script = scripts[0][1].trimEnd().slice(0, -startup.length);
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(root, 'test_frontend.js'), 'utf8'), context);
vm.runInContext(script, context);
vm.runInContext('runFrontendTests()', context).then(
  result => console.log(result),
  error => { console.error(error); process.exitCode = 1; }
);
