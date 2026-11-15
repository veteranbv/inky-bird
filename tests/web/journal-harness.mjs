// Opens one owner journal page against the fake DOM, with stubbed browser
// globals. Shared by the journal unit tests.
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import { Element, ImageElement, fakeDocument } from './fake-dom.mjs';

const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
export const moduleURL = (source) =>
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
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

export const sha = (letter) => letter.repeat(64);
export const bird = (taxon_id, common_name, letter) => ({
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
export function journal() {
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
export const withState = (change) => () => Object.assign(journal(), change);
export const png = () => new Response(new Blob([new Uint8Array([1])], { type: 'image/png' }));
export async function flush() {
  for (let n = 0; n < 20; n++) await setImmediate();
}
export function nodes(node, found = []) {
  for (const child of node.children) {
    found.push(child);
    nodes(child, found);
  }
  return found;
}
export const images = (node) => nodes(node).filter((child) => child instanceof ImageElement);
export const plates = (requests) =>
  requests.filter(({ path }) => /\/(portrait|thumbnail)\?/.test(path));

// Opens one owner page. `state()` is what /v1/owner/journal returns now;
// `profile(id)` answers the bird profile request; `historyState` is the
// browser's saved state for this history entry. Problems the page reports
// fail the test unless `recordProblems` collects them for it to check.
export async function open(
  t,
  query,
  {
    state = journal,
    plate = () => Promise.resolve(png()),
    profile = () => ({ profile: null, notice: 'No field notes.' }),
    historyState = null,
    recordProblems = false,
    respond = null,
  } = {},
) {
  if (!timed.has(t)) t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  timed.add(t);
  const names = ['document', 'window', 'location', 'fetch', 'navigator', 'history'];
  const saved = Object.fromEntries(
    names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const doc = fakeDocument();
  const header = new Element('header');
  const main = new Element('main');
  main.id = 'main';
  doc.body.append(header, main);
  const requests = [];
  const assigned = [];
  const problems = [];
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  const place = {
    href: 'https://inky.example.test/app?' + new URLSearchParams(query),
    reload() {},
    assign(url) {
      assigned.push(url);
    },
  };
  const browserHistory = {
    state: historyState,
    scrollRestoration: 'auto',
    entries: [],
    replaceState(value, _unused, url) {
      this.state = value;
      if (url !== undefined) place.href = new URL(url, place.href).href;
      this.entries.push(place.href);
    },
    pushState() {
      throw new Error('the journal must not add history entries');
    },
  };
  define('document', doc);
  define('window', { isSecureContext: false });
  define('navigator', {});
  define('location', place);
  define('history', browserHistory);
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
  const api = async (path, body) => {
    requests.push({ path, options: {}, body });
    if (path === '/v1/owner/journal') return state();
    const answer = path.match(/^\/v1\/owner\/journal\/birds\/(\d+)\/profile$/);
    if (answer) return profile(Number(answer[1]));
    if (respond) return respond(path, body);
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
      if (!recordProblems) throw error;
      problems.push(error);
    },
    sessionExpired() {},
  };
  loads += 1;
  const { openJournal, closeJournal } = await import(
    moduleURL(journalSource + '\n// load ' + loads)
  );
  close = closeJournal;
  try {
    await openJournal(ui);
  } catch (error) {
    if (!recordProblems) throw error;
    problems.push(error);
  }
  await flush();
  return { doc, main, requests, assigned, problems, history: browserHistory, location: place };
}
