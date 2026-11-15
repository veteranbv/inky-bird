import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

// The whole shipped app.js against a stub journal page, so a signed-in
// render failure can be produced on demand.
const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const moduleURL = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const journalStub = moduleURL(`
export async function openJournal(ui, message) { return globalThis.testJournal(ui, message); }
export function closeJournal() {}
export function retainSignedOutFrame() { return false; }
`);
const uiModule = moduleURL(await readFile(new URL('ui.js', web), 'utf8'));
const appSource = (await readFile(new URL('app.js', web), 'utf8'))
  .replace("'/app/journal.js'", JSON.stringify(journalStub))
  .replace("'/app/ui.js'", JSON.stringify(uiModule));
let loads = 0;

const access = {
  secure: true,
  setup_required: false,
  local_login: true,
  encrypted: false,
  session_policy: { remembered_days: 30, regular_hours: 12 },
  password_min_length: 12,
  password_max_length: 128,
};
const json = (data, status = 200) =>
  new Response(JSON.stringify(status < 400 ? { ok: true, data } : { ok: false, error: data }), {
    status,
  });
async function flush() {
  for (let n = 0; n < 30; n++) await setImmediate();
}

async function open(t, journal, { offline = false } = {}) {
  const names = ['document', 'window', 'location', 'history', 'fetch', 'requestAnimationFrame'];
  const saved = Object.fromEntries(
    names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const doc = fakeDocument();
  doc.createTextNode = (text) => Object.assign(new Element('#text'), { text });
  const main = new Element('main');
  main.id = 'main';
  doc.body.append(new Element('header'), main);
  const requests = [],
    addresses = [];
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  const place = { href: 'https://inky.example.test/app?page=settings', hash: '' };
  define('document', doc);
  define('window', { addEventListener() {} });
  define('location', place);
  define('history', {
    replaceState(_state, _title, url) {
      addresses.push(url);
      place.href = 'https://inky.example.test' + url;
    },
    pushState() {},
    back() {},
  });
  define('requestAnimationFrame', (callback) => callback());
  define('fetch', async (path, options = {}) => {
    requests.push({ path, method: options.method || 'GET' });
    if (offline) throw new TypeError('network');
    if (path === '/v1/owner/slow')
      return new Promise((_resolve, reject) =>
        options.signal.addEventListener('abort', () => reject(new DOMException('', 'AbortError'))),
      );
    if (path === '/v1/access') return json(access);
    if (path === '/v1/owner/session') return json({ csrf: 'token', single_sign_on: false });
    if (path === '/v1/owner/logout') return json({});
    return json({ code: 'not_found', message: 'Not found' }, 404);
  });
  globalThis.testJournal = journal;
  t.after(() => {
    delete globalThis.testJournal;
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
  });
  loads += 1;
  await import(moduleURL(appSource + '\n// load ' + loads));
  await flush();
  return { main, requests, addresses };
}

test('a signed-in page that fails to draw offers Home and sign-out, not only Try again', async (t) => {
  let renders = 0;
  const { main, requests, addresses } = await open(t, async () => {
    renders += 1;
    if (renders === 1) throw new Error('This page could not be drawn.');
  });
  assert.equal(main.querySelector('h1').textContent, 'Inky needs attention');
  assert.ok(findButton(main, 'Try again'));
  const home = findButton(main, 'Go to Home');
  assert.ok(home, 'Home is offered');
  assert.ok(findButton(main, 'Sign out'), 'sign-out is offered');
  home.fire('click');
  await flush();
  assert.deepEqual(addresses, ['/app']);
  assert.equal(renders, 2, 'Home draws the journal again');
});

test('sign-out from the problem page signs out', async (t) => {
  const { main, requests } = await open(t, async () => {
    throw new Error('This page could not be drawn.');
  });
  findButton(main, 'Sign out').fire('click');
  await flush();
  assert.ok(requests.some((item) => item.path === '/v1/owner/logout' && item.method === 'POST'));
});

test('a failure before sign-in offers only Try again', async (t) => {
  const { main } = await open(t, async () => {}, { offline: true });
  assert.equal(main.querySelector('h1').textContent, 'Inky needs attention');
  assert.ok(findButton(main, 'Try again'));
  assert.equal(findButton(main, 'Go to Home'), null);
  assert.equal(findButton(main, 'Sign out'), null);
});

test('a change that timed out says it may have been saved', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let pending;
  await open(t, async (ui) => {
    pending = [ui.api('/v1/owner/slow'), ui.api('/v1/owner/slow', {})].map((call) =>
      call.then(
        () => null,
        (error) => error,
      ),
    );
  });
  t.mock.timers.tick(60000);
  const [read, write] = await Promise.all(pending);
  assert.equal(read.code, 'timeout');
  assert.equal(write.code, 'timeout');
  assert.equal(read.message, 'Inky did not answer in time. Check your connection and try again.');
  assert.equal(
    write.message,
    'Inky did not answer in time. The change may still have been saved, so reload before trying again.',
  );
});
