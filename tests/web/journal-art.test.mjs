import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, ImageElement, fakeDocument, findButton } from './fake-dom.mjs';

const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const moduleURL = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const stubs = {
  '/app/settings.js': 'export function renderSettings() {}',
  '/app/imports.js': 'export function renderImports() {}',
  '/app/presentation.js': 'export function renderPresentation() {}',
};
// A data: module cannot resolve '/app/' paths, so every module it imports,
// and theirs in turn, is inlined as a data: URL too.
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
const timed = new WeakSet();

const sha = (letter) => letter.repeat(64);
const bird = (taxon_id, common_name, letter) => ({
  taxon_id,
  common_name,
  scientific_name: 'Exemplum ' + common_name.toLowerCase(),
  known: true,
  saved: false,
  plate_ready: true,
  sources: [],
  portrait_url: `/v1/owner/journal/birds/${taxon_id}/portrait`,
  portrait_sha256: sha(letter),
});
function journal() {
  return {
    revision: 3,
    birds: [bird(1, 'Example Wren', 'a'), bird(2, 'Sample Finch', 'b'), bird(3, 'Test Heron', 'c')],
    collections: [],
    saved_taxon_ids: [],
    active_taxon_ids: [1, 2, 3],
    featured_taxon_id: 1,
    management: { available: false },
    history_coverage: [],
    unresolved_history: [],
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
function images(node, found = []) {
  for (const child of node.children) {
    if (child instanceof ImageElement) found.push(child);
    images(child, found);
  }
  return found;
}

// Opens one owner page. `state()` is what /v1/owner/journal returns now.
async function open(t, query, { state = journal, plate = () => Promise.resolve(png()) } = {}) {
  if (!timed.has(t)) t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  timed.add(t);
  const names = ['document', 'window', 'location', 'fetch', 'navigator'];
  const saved = Object.fromEntries(
    names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const doc = fakeDocument();
  const header = new Element('header');
  const main = new Element('main');
  main.id = 'main';
  doc.body.append(header, main);
  const requests = [];
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  define('document', doc);
  define('window', { isSecureContext: false });
  define('navigator', {});
  define('location', {
    href: 'https://inky.example.test/app?' + new URLSearchParams(query),
    reload() {},
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
  const api = async (path) => {
    requests.push({ path, options: {} });
    if (path === '/v1/owner/journal') return state();
    if (path.endsWith('/profile')) return { profile: null, notice: 'No field notes.' };
    throw new Error('unexpected ' + path);
  };
  const element = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const ui = {
    api,
    element,
    button(text, action) {
      const node = element('button', text, 'secondary');
      node.addEventListener('click', action);
      return node;
    },
    screen(title, description) {
      main.replaceChildren(element('h1', title), element('p', description, 'intro'));
    },
    showProblem(error) {
      throw error;
    },
    sessionExpired() {},
  };
  loads += 1;
  const { openJournal, closeJournal } = await import(
    moduleURL(journalSource + '\n// load ' + loads)
  );
  close = closeJournal;
  await openJournal(ui);
  await flush();
  return { doc, main, requests };
}
const plates = (requests) => requests.filter(({ path }) => /\/(portrait|thumbnail)\?/.test(path));

test('grid cards ask for the small plate lazily and reserve its size', async (t) => {
  const { main } = await open(t, { page: 'birds' });
  const cards = images(main.querySelector('.bird-grid'));
  assert.equal(cards.length, 3);
  for (const [index, image] of cards.entries()) {
    const id = index + 1;
    assert.equal(image.src, `/v1/owner/journal/birds/${id}/thumbnail?revision=${sha('abc'[index])}`);
    assert.equal(image.width, 480);
    assert.equal(image.height, 640);
    for (const name of ['loading', 'decoding', 'width', 'height'])
      assert(
        image.assigned.indexOf(name) < image.assigned.indexOf('src'),
        name + ' is set before src',
      );
    assert.equal(image.loading, 'lazy');
  }
});

test('collection covers use the small plate', async (t) => {
  const { main } = await open(t, { page: 'collections' });
  const covers = images(main);
  assert(covers.length > 0);
  for (const image of covers) assert.match(image.src, /\/thumbnail\?revision=[a-f]{64}$/);
});

test('search hides cards instead of rebuilding them', async (t) => {
  const { main } = await open(t, { page: 'birds' });
  const grid = main.querySelector('.bird-grid');
  const search = main.querySelector('input');
  const count = () => main.children.find((node) => node.getAttribute('role') === 'status');
  const cards = [...grid.children];
  const pictures = images(grid);
  search.value = 'finch';
  search.fire('input');
  assert.deepEqual([...grid.children], cards, 'the same card nodes stay in place');
  assert.deepEqual(
    cards.map((card) => card.hidden),
    [true, false, true],
  );
  assert.equal(count().textContent, '1 species');
  search.value = 'no such bird';
  search.fire('input');
  assert(cards.every((card) => card.hidden));
  assert.equal(grid.children.length, 4, 'one empty notice joins the hidden cards');
  assert.equal(count().textContent, '0 species');
  search.value = '';
  search.fire('input');
  assert.deepEqual([...grid.children], cards);
  assert(cards.every((card) => !card.hidden));
  assert.deepEqual(images(grid), pictures, 'no image is recreated');
  assert(pictures.every((image) => image.assigned.filter((name) => name === 'src').length === 1));
});

for (const [name, query, selector, letter] of [
  ['Home', { page: 'home' }, '.featured-art', 'a'],
  ['the bird page', { page: 'bird', bird: 2 }, '.detail-art', 'b'],
])
  test(name + ' shows the full plate at its natural size', async (t) => {
    const { main } = await open(t, query);
    const [image] = images(main).filter(
      (node) => node.classList.contains('detail-art') || main.querySelector(selector)?.contains(node),
    );
    assert(image, selector);
    const id = query.bird || 1;
    assert.equal(image.src, `/v1/owner/journal/birds/${id}/portrait?revision=${sha(letter)}`);
    assert.equal(image.width, 1200);
    assert.equal(image.height, 1600);
    assert(image.assigned.indexOf('height') < image.assigned.indexOf('src'));
  });

test('Frame fetches the full revisioned plate and lets the browser cache it', async (t) => {
  const { requests } = await open(t, { page: 'frame', view: 'current' });
  const [first] = plates(requests);
  assert.equal(first.path, `/v1/owner/journal/birds/1/portrait?revision=${sha('a')}`);
  assert.equal(first.options.cache, undefined, 'no cache bypass');
  assert(plates(requests).every(({ path }) => !path.includes('thumbnail')));
});

test('Frame checks the journal only while visible and leaves an unchanged view alone', async (t) => {
  let state = journal();
  let slow = false;
  const { doc, main, requests } = await open(t, { page: 'frame', view: 'current' }, {
    state: () => state,
    plate: (path, options) => (slow ? hang(options) : Promise.resolve(png())),
  });
  const checks = () => requests.filter(({ path }) => path === '/v1/owner/journal').length;
  const shown = images(main).find((image) => image.classList.contains('frame-art'));
  assert.equal(checks(), 1);
  doc.hidden = true;
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(checks(), 1, 'a hidden Frame does not check');
  doc.hidden = false;
  // A slow Next stays in flight; an unchanged check must not cancel it.
  slow = true;
  findButton(main, 'Next').click();
  await flush();
  const pending = plates(requests).at(-1);
  assert.equal(pending.path, `/v1/owner/journal/birds/2/portrait?revision=${sha('b')}`);
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(checks(), 2);
  assert.equal(pending.options.signal.aborted, false, 'the unchanged check leaves Next running');
  assert(shown.isConnected, 'the plate on screen stays');
  // A changed plate is picked up with its new revision.
  slow = false;
  state = journal();
  state.revision = 4;
  state.birds[0].portrait_sha256 = sha('d');
  t.mock.timers.tick(60000);
  await flush();
  assert(
    plates(requests).some(({ path }) => path === `/v1/owner/journal/birds/1/portrait?revision=${sha('d')}`),
  );
});

test('an unchanged check after an outage clears the offline notice', async (t) => {
  let offline = false;
  const { main, requests } = await open(t, { page: 'frame', view: 'current' }, {
    state: () => {
      if (offline) throw Object.assign(new Error('offline'), { code: 'offline' });
      return journal();
    },
  });
  const status = () => main.querySelector('.frame-dock').children[0].textContent;
  offline = true;
  t.mock.timers.tick(60000);
  await flush();
  assert.match(status(), /^Offline\./);
  offline = false;
  t.mock.timers.tick(60000);
  await flush();
  assert.equal(status(), '');
  assert.equal(plates(requests).length, 1, 'the plate on screen is not fetched again');
});

// Parts the controller could not read reach the owner as plain text.
function nodes(node, found = []) {
  for (const child of node.children) {
    found.push(child);
    nodes(child, found);
  }
  return found;
}
const notices = (main) =>
  nodes(main).filter((node) => node.classList.contains('notice') && node.getAttribute('role') === 'status');
const withState = (change) => () => Object.assign(journal(), change);
const markup = 'Plates waiting for review could not be read. <img src=x onerror=alert(1)>';

for (const page of ['home', 'birds'])
  test(page + ' shows each unreadable part as text', async (t) => {
    const { main } = await open(t, { page }, {
      state: withState({
        notices: [
          { part: 'plates-in-review', message: markup },
          { part: 'plate-queue', message: 'The list of birds waiting for plates could not be read.' },
        ],
        history_coverage: [
          { source: 'ebird-archive', status: 'unavailable', coverage: 'Retained history needs attention.' },
          { source: 'birdbuddy', status: 'available', coverage: 'Retained postcards', species_count: 1, unit: 'postcards' },
        ],
      }),
    });
    const [box, ...extra] = notices(main);
    assert(box, 'a notice is shown');
    assert.equal(extra.length, 0, 'one notice holds every message');
    assert.deepEqual(
      box.children.map((line) => line.textContent),
      [markup, 'The list of birds waiting for plates could not be read.', 'eBird Archive: Retained history needs attention.'],
    );
    for (const node of [box, ...nodes(box)]) {
      assert.equal(node.html, undefined, 'no markup is parsed');
      assert(node.children.every((child) => child.tagName === 'P'), 'messages stay text');
    }
  });

for (const [name, change] of [
  ['an empty list', { notices: [] }],
  ['an older controller without notices', {}],
])
  for (const page of ['home', 'birds'])
    test(page + ' shows no notice for ' + name, async (t) => {
      const { main } = await open(t, { page }, { state: withState(change) });
      assert.deepEqual(notices(main), []);
    });

test('plate work that could not be read says so instead of asking for setup', async (t) => {
  const message = 'Plate work could not be read. Manage shows it again once its files are readable.';
  const { main } = await open(t, { page: 'work' }, {
    state: withState({
      management: { available: false, reason: 'unreadable' },
      notices: [{ part: 'plate-work', message }],
    }),
  });
  const text = main.textContent;
  assert(!text.includes('Full configuration unavailable'));
  assert(!text.includes('documented serve command'));
  const [box] = notices(main);
  assert.equal(box?.textContent, message);
});

test('unreadable plate work without its notice still says it could not be read', async (t) => {
  const { main } = await open(t, { page: 'sources' }, {
    state: withState({ management: { available: false, reason: 'unreadable' } }),
  });
  assert(!main.textContent.includes('Full configuration unavailable'));
  assert.equal(notices(main)[0]?.textContent, 'Plate work could not be read.');
});

test('an install without plate work configured keeps its setup message', async (t) => {
  const { main } = await open(t, { page: 'work' });
  assert(main.textContent.includes('Full configuration unavailable'));
  assert(main.textContent.includes('Start Inky through its documented serve command'));
  assert.deepEqual(notices(main), []);
});
