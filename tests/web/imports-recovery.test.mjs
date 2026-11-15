import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test, { mock } from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

// Recovery paths of the history import page. Helpers match imports.test.mjs.

const source = await readFile(
  new URL('../../src/inky_bird_frame/web/imports.js', import.meta.url),
  'utf8',
);
const { renderImports } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);

const HOUR = 3600 * 1000;

function text(node) {
  return node.textContent;
}

function findAll(root, predicate, found = []) {
  for (const child of root.children) {
    if (predicate(child)) found.push(child);
    findAll(child, predicate, found);
  }
  return found;
}

class RequestError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

// Answers each path from `routes`; a function answer gets the request body,
// an Error answer is thrown. Every call is recorded.
function setup(routes, { kind = 'ebird-archive' } = {}) {
  const document = fakeDocument();
  const main = new Element('main');
  main.id = 'main';
  document.body.append(main);
  document.createTextNode = (content) => {
    const node = new Element('#text');
    node.textContent = content;
    return node;
  };
  globalThis.document = document;
  const calls = [];
  const actions = [];
  const api = async (path, body) => {
    calls.push({ path, body });
    const answer = routes[path];
    if (answer === undefined) throw new Error('unexpected request ' + path);
    const value = typeof answer === 'function' ? await answer(body) : answer;
    if (value instanceof Error) throw value;
    return structuredClone(value);
  };
  const uploads = [];
  const ui = {
    element(tag, content, className) {
      const node = new Element(tag);
      if (content) node.textContent = content;
      if (className) node.className = className;
      return node;
    },
    button(content, action) {
      const node = new Element('button');
      node.textContent = content;
      node.type = 'button';
      // Keep the click's promise so a test can see whether it rejected.
      node.addEventListener('click', (event) => actions.push(action(event)));
      return node;
    },
    api,
    async upload(path, file, signal, onProgress) {
      uploads.push({ path, file, signal, onProgress });
      return routes.upload ? routes.upload(onProgress) : {};
    },
    showProblem() {},
  };
  const helpers = {
    base(title, intro) {
      main.replaceChildren(ui.element('h1', title), ui.element('p', intro));
    },
    link(label) {
      return ui.element('a', label);
    },
    note(content) {
      return ui.element('p', content, 'note');
    },
    kind,
  };
  return { main, calls, actions, ui, helpers, uploads };
}

async function click(node) {
  assert.ok(node, 'button exists');
  node.fire('click', { currentTarget: node });
  await setImmediate();
}

async function settled(actions) {
  const results = await Promise.allSettled(actions);
  for (const result of results) assert.equal(result.status, 'fulfilled', String(result.reason));
}

const uploaded = (overrides = {}) => ({
  preview: {
    upload_id: 'a'.repeat(32),
    kind: 'ebird-archive',
    status: 'uploaded',
    expires_at: new Date(Date.now() + HOUR).toISOString(),
    ...overrides,
  },
});

test('a stopped upload reads as stopped, not as a network fault', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': { preview: null },
    upload: () => {
      throw new RequestError('Upload stopped. No history changed.', 'stopped');
    },
  });
  await renderImports(env.ui, env.helpers);
  const form = findAll(env.main, (node) => node.tagName === 'FORM')[0];
  findAll(form, (node) => node.tagName === 'INPUT')[0].files = [{ size: 10 }];
  form.fire('submit', {});
  await setImmediate();
  const alerts = findAll(env.main, (node) => node.getAttribute('role') === 'alert');
  assert.deepEqual(alerts.map(text).filter(Boolean), [], 'no error is raised');
  const status = findAll(form, (node) => node.getAttribute('role') === 'status')[0];
  assert.equal(text(status), 'Upload stopped. No history changed.');
  assert.equal(findButton(form, 'Check saved upload').hidden, false);
});

test('a page that cannot read its saved state says so in place', async () => {
  let fail = true;
  const env = setup({
    '/v1/owner/operations': () =>
      fail ? new RequestError('Inky cannot be reached.', 'offline') : { operations: [] },
    '/v1/owner/imports': { preview: null },
  });
  const stop = await renderImports(env.ui, env.helpers);
  assert.equal(typeof stop, 'function', 'the page still returns its stop control');
  assert.equal(env.main.children[0].textContent, 'Your eBird history', 'the page stays');
  const alerts = findAll(env.main, (node) => node.getAttribute('role') === 'alert');
  assert.ok(alerts.some((node) => text(node) === 'Inky cannot be reached.'));
  fail = false;
  await click(findButton(env.main, 'Check saved state'));
  await settled(env.actions);
  assert.ok(findAll(env.main, (node) => node.tagName === 'FORM').length, 'the file step appears');
});

test('a refused step is sent again with fresh choices, a lost reply with the same ones', async () => {
  let answer = new RequestError('Settings changed. Reload and try again.', 'journal_changed');
  let revision = 'r1';
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': uploaded(),
    '/v1/owner/settings': () => ({ revision }),
    '/v1/owner/imports/import-preview': () => answer,
  });
  await renderImports(env.ui, env.helpers);
  const sent = () => env.calls.filter(({ path }) => path === '/v1/owner/imports/import-preview');
  await click(findButton(env.main, 'Check this file'));
  revision = 'r2';
  answer = new RequestError('Inky cannot be reached.', 'offline');
  await click(findButton(env.main, 'Check this file'));
  answer = new RequestError('Inky did not answer in time.', 'timeout');
  await click(findButton(env.main, 'Check this file'));
  await settled(env.actions);
  const [refused, lost, again] = sent().map(({ body }) => body);
  assert.equal(refused.revision, 'r1');
  assert.equal(lost.revision, 'r2', 'a refused request is rebuilt');
  assert.notEqual(lost.key, refused.key, 'a refused request gets a new key');
  assert.deepEqual(again, lost, 'an unknown outcome is retried unchanged');
});

test('a step error stays visible after the step was replaced', async () => {
  let reads = 0;
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': () =>
      ++reads > 1 ? new RequestError('Saved state could not be read.', 'journal_error') : uploaded(),
    '/v1/owner/settings': { revision: 'r1' },
    '/v1/owner/imports/import-preview': { operation_id: 'op-1' },
    '/v1/owner/operations/op-1': { status: 'succeeded', result: {} },
  });
  await renderImports(env.ui, env.helpers);
  await click(findButton(env.main, 'Check this file'));
  await settled(env.actions);
  const alerts = findAll(env.main, (node) => node.getAttribute('role') === 'alert');
  assert.ok(alerts.some((node) => text(node) === 'Saved state could not be read.'));
});

test('a step the controller accepted keeps its key when its status cannot be read', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': uploaded(),
    '/v1/owner/settings': { revision: 'r1' },
    '/v1/owner/imports/import-preview': { operation_id: 'op-1' },
    '/v1/owner/operations/op-1': new RequestError(
      'Inky could not complete this request.',
      'internal',
    ),
  });
  await renderImports(env.ui, env.helpers);
  await click(findButton(env.main, 'Check this file'));
  await click(findButton(env.main, 'Check this file'));
  await settled(env.actions);
  const [first, second] = env.calls
    .filter(({ path }) => path === '/v1/owner/imports/import-preview')
    .map(({ body }) => body);
  assert.ok(second, 'the step can be pressed again');
  assert.deepEqual(second, first, 'the accepted request is sent again unchanged');
});
