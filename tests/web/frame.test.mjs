import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

// Owner Frame through the shipped journal page, with every '/app/' module
// inlined as a data: URL. The three form areas are stubbed.
const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const moduleURL = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const stubs = {
  '/app/settings.js': 'export function renderSettings() {}',
  '/app/imports.js': 'export function renderImports() {}',
  '/app/presentation.js': 'export function renderPresentation() {}',
};
const inlined = new Map();
async function inline(source) {
  for (const [, path] of source.matchAll(/from '(\/app\/[\w-]+\.js)'/g)) {
    if (!inlined.has(path))
      inlined.set(
        path,
        moduleURL(stubs[path] ?? (await inline(await readFile(new URL(path.slice(5), web), 'utf8')))),
      );
    source = source.replace(`'${path}'`, JSON.stringify(inlined.get(path)));
  }
  return source;
}
const journalSource = await inline(await readFile(new URL('journal.js', web), 'utf8'));
let loads = 0;

const sha = (letter) => letter.repeat(64);
const bird = (taxon_id, common_name, letter, plate_ready = true) => ({
  taxon_id,
  common_name,
  scientific_name: 'Exemplum ' + common_name.toLowerCase(),
  known: true,
  saved: false,
  plate_ready,
  sources: [],
  portrait_sha256: sha(letter),
});
function journal(active = [1, 2]) {
  return {
    revision: 3,
    birds: [bird(1, 'Example Wren', 'a'), bird(2, 'Sample Finch', 'b')],
    collections: [],
    saved_taxon_ids: [],
    active_taxon_ids: active,
    management: { available: false },
    history_coverage: [],
  };
}
const png = () => new Response(new Blob([new Uint8Array([1])], { type: 'image/png' }));
const hang = (options) =>
  new Promise((_resolve, reject) =>
    options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))),
  );
async function flush() {
  for (let n = 0; n < 20; n++) await setImmediate();
}

async function open(t, { state, plate = () => Promise.resolve(png()), secure = true }) {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  const names = ['document', 'window', 'location', 'fetch', 'navigator'];
  const saved = Object.fromEntries(
    names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const doc = fakeDocument();
  const main = new Element('main');
  main.id = 'main';
  doc.body.append(new Element('header'), main);
  const requests = [],
    reloads = [],
    problems = [];
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  define('document', doc);
  define('window', { isSecureContext: secure });
  define('navigator', {});
  define('location', {
    href: 'https://inky.example.test/app?page=frame&view=current',
    reload: () => reloads.push(true),
    assign() {},
  });
  define('fetch', (path, options = {}) => {
    requests.push({ path, options });
    return plate(path, options);
  });
  let close = () => {};
  t.after(() => {
    close();
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
  });
  const element = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const ui = {
    api: async (path) => {
      requests.push({ path, options: {} });
      if (path === '/v1/owner/journal') return state();
      throw new Error('unexpected ' + path);
    },
    element,
    button(text, action) {
      const node = element('button', text, 'secondary');
      node.addEventListener('click', action);
      return node;
    },
    screen(title, description) {
      main.replaceChildren(element('h1', title), element('p', description, 'intro'));
    },
    showProblem: (error) => problems.push(error),
    sessionExpired() {},
  };
  loads += 1;
  const { openJournal, closeJournal } = await import(
    moduleURL(journalSource + '\n// load ' + loads)
  );
  close = closeJournal;
  await openJournal(ui);
  await flush();
  return { main, requests, reloads, problems, leave: closeJournal };
}
const checks = (requests) => requests.filter(({ path }) => path === '/v1/owner/journal').length;
const status = (main) => main.querySelector('.frame-dock')?.children[0].textContent;

test('Frame opened on an empty view keeps checking and starts when plates arrive', async (t) => {
  let state = journal([]);
  const { main, requests, reloads } = await open(t, { state: () => state });
  assert.match(main.textContent, /No plates here yet/);
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(checks(requests), 2, 'the empty view is checked again');
  assert.equal(reloads.length, 0, 'still empty: nothing restarts');
  state = journal([2]);
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(reloads.length, 0, 'the page redraws in place');
  assert.doesNotMatch(main.textContent, /No plates here yet/, 'plates that arrive start Frame again');
});

test('a failed check on an empty view keeps checking', async (t) => {
  let offline = false,
    calls = 0;
  const { main, reloads, problems } = await open(t, {
    state: () => {
      calls += 1;
      if (offline) throw Object.assign(new Error('offline'), { code: 'offline' });
      return calls > 2 ? journal([1]) : journal([]);
    },
  });
  offline = true;
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(problems.length, 0, 'a transient failure does not replace the page');
  offline = false;
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(reloads.length, 0);
  assert.doesNotMatch(main.textContent, /No plates here yet/);
});

test('an ended session on an empty view goes to sign-in', async (t) => {
  let ended = false;
  const { problems } = await open(t, {
    state: () => {
      if (ended) throw Object.assign(new Error('Sign in again.'), { code: 'sign_in_required' });
      return journal([]);
    },
  });
  ended = true;
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(problems.length, 1);
  assert.equal(problems[0].code, 'sign_in_required');
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(problems.length, 1, 'checking stops once the session ended');
});

for (const [outcome, answer] of [
  ['ready plates', (resolve) => resolve(journal([1]))],
  ['an ended session', (_resolve, reject) =>
    reject(Object.assign(new Error('Sign in again.'), { code: 'sign_in_required' }))],
]) {
  test(`a check on an empty view that returns ${outcome} after the owner left does nothing`, async (t) => {
    let pending = null;
    const { reloads, problems, leave } = await open(t, {
      state: () => (pending ? new Promise((resolve, reject) => pending.push([resolve, reject])) : journal([])),
    });
    pending = [];
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(pending.length, 1, 'a check is waiting for its answer');
    leave();
    answer(...pending[0]);
    await flush();
    assert.equal(reloads.length, 0, 'the page the owner moved to is not reloaded');
    assert.equal(problems.length, 0, 'the page the owner moved to is not replaced');
  });
}

test('a hung plate request ends at its deadline and says so', async (t) => {
  const { main, requests } = await open(t, { state: () => journal(), plate: (_path, options) => hang(options) });
  const [first] = requests.filter(({ path }) => path.includes('/portrait'));
  assert.equal(status(main), '');
  // Hidden, so the minute check does not start the plate again.
  document.hidden = true;
  t.mock.timers.tick(89999);
  await flush();
  assert.equal(first.options.signal.aborted, false, 'a slow plate gets its full time');
  t.mock.timers.tick(1);
  await flush();
  assert.equal(first.options.signal.aborted, true, 'the request is ended');
  assert.match(status(main), /^This plate did not load\./);
});

test('a plate that arrives in time is shown as before', async (t) => {
  const { main } = await open(t, { state: () => journal() });
  t.mock.timers.tick(90000);
  await flush();
  assert.equal(status(main), '');
  assert.match(main.querySelector('.frame-caption').textContent, /Example Wren/);
  assert.ok(findButton(main, 'Next'));
  assert.ok(main.children.some((node) => node.classList.contains('frame-viewport')));
});

// Keeping the screen awake needs a secure context, which plain HTTP is not.
test('Frame over plain HTTP says the screen may sleep', async (t) => {
  const plain = await open(t, { state: () => journal(), secure: false });
  assert.match(plain.main.textContent, /keeping it awake needs an HTTPS address/);
});

test('Frame in a secure context does not warn about sleep', async (t) => {
  const { main } = await open(t, { state: () => journal() });
  assert.doesNotMatch(main.textContent, /keeping it awake/);
});

// The "why this bird" line under the plate comes from the page's shared helper
// when it has one; Play shows nothing in its place otherwise.
const { renderFrame } = await import(
  moduleURL(await inline(await readFile(new URL('frame.js', web), 'utf8')))
);
async function play(t, whyThisBird) {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  const names = ['document', 'window', 'fetch'];
  const saved = Object.fromEntries(
    names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  const doc = fakeDocument();
  const main = new Element('main');
  doc.body.append(main);
  define('document', doc);
  define('window', { isSecureContext: true });
  define('fetch', () => Promise.resolve(png()));
  let stop = () => {};
  t.after(() => {
    stop();
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
  });
  const el = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const state = journal();
  renderFrame({
    ui: { showProblem() {}, sessionExpired() {} },
    el,
    button: (text, action) => {
      const node = el('button', text, 'secondary');
      node.addEventListener('click', action);
      return node;
    },
    api: async () => state,
    main,
    view: 'current',
    data: state,
    byId: new Map(),
    plateURL: (bird) => '/plate/' + bird.taxon_id,
    href: () => '/app',
    link: (text) => el('a', text, 'text-link'),
    note: (text) => el('p', text, 'hint'),
    members: () => state.birds,
    viewLabel: () => 'On my frame',
    base: (title) => main.replaceChildren(el('h1', title)),
    empty: (title) => el('section', title),
    closeJournal() {},
    setStopScreen: (callback) => {
      stop = callback;
    },
    setFrameSignOut() {},
    ...(whyThisBird ? { whyThisBird } : {}),
  });
  await flush();
  return main.querySelector('.frame-why');
}

test('Play shows the why-this-bird line its helper gives', async (t) => {
  const why = await play(t, (bird) => (bird.taxon_id === 1 ? 'Noted at your feeder this week' : ''));
  assert.equal(why.textContent, 'Noted at your feeder this week');
  assert.equal(why.hidden, false);
});

for (const [name, helper] of [
  ['without a helper', null],
  ['when its helper has nothing to say', () => ''],
])
  test('Play leaves the why-this-bird line out ' + name, async (t) => {
    const why = await play(t, helper);
    assert.equal(why.textContent, '');
    assert.equal(why.hidden, true);
  });

// The journal page hands Play the same reason Home shows for its featured bird.
test('Play under the journal shows why a bird noted this week is shown', async (t) => {
  const now = new Date();
  const today = [now.getFullYear(), now.getMonth() + 1, now.getDate()]
    .map((part) => String(part).padStart(2, '0'))
    .join('-');
  const state = () => {
    const value = journal();
    value.birds[0].retained_evidence = [
      { source: 'birdbuddy', count: 2, unit: 'postcard', first_noted: '2019-03-12', last_noted: today },
    ];
    return value;
  };
  const { main } = await open(t, { state });
  const why = main.querySelector('.frame-why');
  assert.equal(why.textContent, 'Noted at your feeder this week');
  assert.equal(why.hidden, false);
});
