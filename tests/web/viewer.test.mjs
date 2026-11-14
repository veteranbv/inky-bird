import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const toolsURL =
  'data:text/javascript;base64,' +
  Buffer.from(await readFile(new URL('frame-tools.js', web), 'utf8')).toString('base64');
const viewerSource = (await readFile(new URL('viewer.js', web), 'utf8')).replace(
  "'/app/frame-tools.js'",
  JSON.stringify(toolsURL),
);
let loads = 0;

const bird = {
  taxon_id: 1,
  common_name: 'Example Wren',
  portrait_sha256: 'a'.repeat(64),
  portrait_url: '/v1/screen/plates/1?revision=' + 'a'.repeat(64),
};
const json = (data, status = 200) =>
  new Response(JSON.stringify(status < 400 ? { ok: true, data } : { ok: false, error: data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
const png = () => new Response(new Blob([new Uint8Array([1])], { type: 'image/png' }));
// A request that only ends when its signal aborts, like a hung connection.
const hang = (options) =>
  new Promise((_resolve, reject) =>
    options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))),
  );

async function flush() {
  for (let n = 0; n < 20; n++) await setImmediate();
}

// Loads a fresh copy of the screen page against `route(path, options)`.
async function open(t, route) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const saved = {};
  for (const name of ['document', 'window', 'location', 'fetch', 'Image', 'addEventListener'])
    saved[name] = Object.getOwnPropertyDescriptor(globalThis, name);
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const doc = fakeDocument();
  const main = new Element('main');
  main.id = 'main';
  doc.body.append(main);
  const requests = [],
    reloads = [];
  Object.assign(globalThis, {
    document: doc,
    window: { isSecureContext: false },
    location: { reload: () => reloads.push(doc.body.classList.contains('presenting')) },
    fetch: (path, options = {}) => {
      requests.push({ path, method: options.method || 'GET' });
      return route(path, options);
    },
    Image: class {
      decode() {
        return Promise.resolve();
      }
    },
    addEventListener: () => {},
  });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} });
  t.after(() => {
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    if (navigatorDescriptor) Object.defineProperty(globalThis, 'navigator', navigatorDescriptor);
    else delete globalThis.navigator;
  });
  loads += 1;
  await import(
    'data:text/javascript;base64,' +
      Buffer.from(viewerSource + '\n// load ' + loads).toString('base64')
  );
  await flush();
  return { doc, main, requests, reloads };
}
const views = (requests) => requests.filter((request) => request.path === '/v1/screen/view').length;
const statusText = (main) => main.querySelector('.hint')?.textContent || '';

test('a failed Forget keeps the screen presenting and polling', async (t) => {
  let forgetFails = true;
  const presentingAtRequest = [];
  const screen = await open(t, (path) => {
    if (path === '/v1/screen/view') return Promise.resolve(json({ species: [bird] }));
    if (path !== '/v1/screen/forget') return Promise.resolve(png());
    presentingAtRequest.push(globalThis.document.body.classList.contains('presenting'));
    return forgetFails
      ? Promise.reject(new TypeError('network'))
      : Promise.resolve(json({ forgotten: true }));
  });
  assert(screen.doc.body.classList.contains('presenting'));
  const image = screen.main.querySelector('img');
  assert(image.src, 'the plate is showing');
  findButton(screen.main, 'Forget this screen').click();
  assert.equal(
    screen.requests.filter((request) => request.path === '/v1/screen/forget').length,
    0,
    'the first press only asks',
  );
  findButton(screen.main, 'Forget').click();
  await flush();
  assert.equal(screen.reloads.length, 0);
  assert(screen.doc.body.classList.contains('presenting'), 'the presentation keeps running');
  assert(image.src, 'the plate stays');
  assert.match(statusText(screen.main), /This screen was not forgotten\. Offline/);
  const before = views(screen.requests);
  t.mock.timers.tick(15000);
  await flush();
  assert.equal(views(screen.requests), before + 1, 'polling continues');
  forgetFails = false;
  findButton(screen.main, 'Forget this screen').click();
  findButton(screen.main, 'Forget').click();
  await flush();
  assert.deepEqual(presentingAtRequest, [true, true], 'nothing is cleared before the request');
  assert.deepEqual(screen.reloads, [false], 'success clears the art, then reloads');
});

test('Forget returns to its first step when the controls fade', async (t) => {
  const screen = await open(t, (path) =>
    path === '/v1/screen/view' ? Promise.resolve(json({ species: [bird] })) : Promise.resolve(png()),
  );
  findButton(screen.main, 'Forget this screen').click();
  assert(!findButton(screen.main, 'Forget').parent.hidden);
  t.mock.timers.tick(5000);
  assert(findButton(screen.main, 'Forget').parent.hidden, 'a later stray tap cannot confirm');
  assert(!findButton(screen.main, 'Forget this screen').hidden);
});

test('a failed Cancel pairing keeps the code and the approval check running', async (t) => {
  let paired = false;
  const screen = await open(t, (path) =>
    path === '/v1/screen/pair'
      ? Promise.resolve(json({ status: 'pending', pairing_code: 'TEST-CODE' }))
      : path === '/v1/screen/forget'
        ? Promise.reject(new TypeError('network'))
        : paired
          ? Promise.resolve(json({ species: [bird] }))
          : Promise.resolve(json({ code: 'sign_in_required', message: 'Pair first.' }, 401)),
  );
  findButton(screen.main, 'Pair this screen').click();
  await flush();
  const cancel = findButton(screen.main, 'Cancel pairing');
  cancel.click();
  await flush();
  assert.equal(screen.reloads.length, 0);
  assert.equal(screen.main.querySelector('.pairing-code').textContent, 'TEST-CODE');
  assert.match(
    screen.main.children.find((node) => node.getAttribute('role') === 'status').textContent,
    /Pairing was not cancelled\./,
  );
  assert(!cancel.disabled, 'Cancel can be tried again');
  paired = true;
  t.mock.timers.tick(15000);
  await flush();
  assert(screen.doc.body.classList.contains('presenting'), 'approval still reaches the screen');
});

test('a hung view request times out and polling resumes', async (t) => {
  let stuck = false;
  const screen = await open(t, (path, options) =>
    path === '/v1/screen/view'
      ? stuck
        ? hang(options)
        : Promise.resolve(json({ species: [bird] }))
      : Promise.resolve(png()),
  );
  stuck = true;
  t.mock.timers.tick(15000);
  await flush();
  assert.equal(views(screen.requests), 2);
  t.mock.timers.tick(20000);
  await flush();
  assert.match(statusText(screen.main), /not responding/);
  stuck = false;
  t.mock.timers.tick(15000);
  await flush();
  assert.equal(views(screen.requests), 3, 'the next check still runs');
  assert.equal(statusText(screen.main), '', 'the notice clears once Inky answers');
});

test('a hung plate request times out without stopping the screen', async (t) => {
  const plates = [];
  const screen = await open(t, (path, options) => {
    if (path === '/v1/screen/view') return Promise.resolve(json({ species: [bird] }));
    plates.push(path);
    return plates.length <= 2 ? hang(options) : Promise.resolve(png());
  });
  // The first check replaces the stuck first plate with a second stuck one.
  t.mock.timers.tick(15000);
  await flush();
  assert.equal(views(screen.requests), 2);
  t.mock.timers.tick(20000);
  await flush();
  assert.match(statusText(screen.main), /temporarily unavailable\. Keeping the last approved image/);
  t.mock.timers.tick(15000);
  await flush();
  assert.equal(views(screen.requests), 3, 'polling continues after the deadline');
  assert(screen.main.querySelector('img').src, 'the next attempt shows the plate');
});

test('previous and next do nothing harmful once the view empties', async (t) => {
  let species = [bird];
  const screen = await open(t, (path) =>
    path === '/v1/screen/view' ? Promise.resolve(json({ species })) : Promise.resolve(png()),
  );
  species = [];
  t.mock.timers.tick(15000);
  await flush();
  findButton(screen.main, 'Next').click();
  findButton(screen.main, 'Previous').click();
  await flush();
  assert(!screen.main.querySelector('img').src);
  assert.match(statusText(screen.main), /No approved plates in the permitted view/);
  species = [bird];
  t.mock.timers.tick(15000);
  await flush();
  assert(screen.main.querySelector('img').src, 'the plate returns without a reload');
});

test('a swipe on a faded wall screen steps, and Space still pauses', async (t) => {
  const other = {
    ...bird,
    taxon_id: 2,
    common_name: 'Example Finch',
    portrait_sha256: 'b'.repeat(64),
    portrait_url: '/v1/screen/plates/2?revision=' + 'b'.repeat(64),
  };
  const screen = await open(t, (path) =>
    path === '/v1/screen/view'
      ? Promise.resolve(json({ species: [bird, other] }))
      : Promise.resolve(png()),
  );
  const controls = screen.main.querySelector('.frame-controls');
  const viewport = screen.main.querySelector('.frame-viewport');
  t.mock.timers.tick(5000);
  assert(controls.classList.contains('quiet'), 'the wall screen has faded its controls');
  screen.doc.fire('pointerdown', { target: viewport, pointerType: 'touch' });
  viewport.fire('pointerdown', { clientX: 300 });
  viewport.fire('pointerup', { clientX: 200 });
  await flush();
  assert(
    screen.requests.some((request) => request.path === other.portrait_url),
    'the first swipe after the fade steps to the next plate',
  );
  t.mock.timers.tick(5000);
  screen.doc.fire('pointerdown', { target: viewport, pointerType: 'touch' });
  viewport.fire('pointerdown', { clientX: 200 });
  viewport.fire('pointerup', { clientX: 202 });
  const play = findButton(screen.main, 'Play');
  assert.equal(play.getAttribute('aria-pressed'), 'true');
  screen.doc.fire('keydown', { target: screen.doc.body, key: ' ' });
  assert.equal(play.getAttribute('aria-pressed'), 'false', 'Space pauses after a tap on the plate');
});
