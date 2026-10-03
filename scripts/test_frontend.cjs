// Development-only runner. No packages or browser installation required.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..', 'dashboard');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace('refresh().then(history);', '')
  .replace('refreshSystem();', '')
  .replace('setInterval(freshness, 1000);', '');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(root, 'test_frontend.js'), 'utf8'), context);
vm.runInContext(script, context);
vm.runInContext('runFrontendTests()', context).then(
  result => console.log(result),
  error => { console.error(error); process.exitCode = 1; }
);
