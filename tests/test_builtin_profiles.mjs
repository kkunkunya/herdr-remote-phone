import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>\s*<\/body>/)?.[1];
assert.ok(script, 'inline app script is missing');

class Storage {
  constructor(values = {}) { this.values = new Map(Object.entries(values)); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

function boot(saved = {}) {
  const elements = new Map();
  const element = () => ({
    style: {}, classList: { add() {}, remove() {} }, value: '', innerHTML: '', textContent: '',
    checked: false, addEventListener() {}, appendChild() {}, focus() {}, scrollTo() {}, querySelectorAll() { return []; },
  });
  const document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element, querySelectorAll() { return []; }, body: element(),
  };
  const sockets = [];
  class WebSocket {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    close() { this.readyState = 3; }
    send(message) { this.sent.push(JSON.parse(message)); }
  }
  const context = {
    console, document, localStorage: new Storage(saved), location: { hostname: 'test.invalid', protocol: 'https:', search: '' },
    navigator: {}, WebSocket, setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {},
    URLSearchParams, JSON, Date, Math, Promise,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(script, context, { filename: 'index.html' });
  return { context, elements, sockets };
}

function value(app, expression) { return vm.runInContext(expression, app.context); }
function connect(app) {
  value(app, 'connect()');
  return app.sockets.at(-1);
}
function assertSocketUsesActiveProfile(app, socket) {
  const profile = JSON.parse(value(app, 'JSON.stringify(activeProfile())'));
  const url = new URL(socket.url);
  assert.equal(url.hostname, profile.host.replace(/^\w+:\/\//, '').replace(/\/$/, ''));
  assert.equal(url.searchParams.get('token'), profile.token);
}

const fresh = boot();
assertSocketUsesActiveProfile(fresh, connect(fresh));
value(fresh, "switchProfile('air')");
assertSocketUsesActiveProfile(fresh, fresh.sockets.at(-1));

const manual = boot();
manual.elements.get('relayUrl').value = 'wss://manual.example';
manual.elements.get('relayToken').value = 'manual-token';
value(manual, 'saveAndConnect()');
assertSocketUsesActiveProfile(manual, manual.sockets.at(-1));

const legacy = boot({ herdr_relay_token: 'legacy-token' });
assert.equal(value(legacy, 'profiles.pro.token'), 'legacy-token');
assert.equal(value(legacy, 'profiles.air.token'), 'legacy-token');

const partial = boot({ herdr_profiles: JSON.stringify({ pro: { host: 'old-pro' }, token: 'legacy-token' }) });
assert.equal(value(partial, 'profiles.pro.host'), 'old-pro');
assert.equal(value(partial, 'profiles.air.token'), 'legacy-token');

const push = boot();
value(push, "pushSubscription = { toJSON() { return { endpoint: 'test' }; } }; connect()");
const pushSocket = push.sockets.at(-1);
pushSocket.onopen();
assert.ok(pushSocket.sent.some(message => message.type === 'push_subscribe'));

console.log('builtin profile behavior passed');
