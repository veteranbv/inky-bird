import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

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

test('a failed recovery check shows its error instead of an unhandled rejection', async () => {
  let fail = false;
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': () =>
      fail ? new RequestError('Inky cannot be reached.', 'offline') : uploaded(),
    '/v1/owner/settings': { revision: 'r1' },
    '/v1/owner/imports/import-preview': { operation_id: 'op-1' },
    '/v1/owner/operations/op-1': { status: 'failed', result: { message: 'Stopped.' } },
  });
  await renderImports(env.ui, env.helpers);
  await click(findButton(env.main, 'Check this file'));
  await settled(env.actions);
  fail = true;
  const recheck = findButton(env.main, 'Check saved state');
  await click(recheck);
  await settled(env.actions);

  const alerts = findAll(env.main, (node) => node.getAttribute('role') === 'alert');
  assert.ok(alerts.some((node) => text(node) === 'Inky cannot be reached.'));
  assert.equal(recheck.disabled, false);
});

test('a failed trip edit shows its error', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': uploaded({
      status: 'committed',
      trip: {
        preview: {
          label: 'Spring trip',
          matched: 2,
          ready: 1,
          unresolved: [],
          unresolved_for_now: 0,
          not_looked_up: [],
          notice: 'Save fixes this trip.',
          preview_hash: 'h',
        },
      },
    }),
    '/v1/owner/imports/trip-edit': new RequestError('This preview changed.', 'conflict'),
  });
  await renderImports(env.ui, env.helpers);
  await click(findButton(env.main, 'Edit trip selection'));
  await settled(env.actions);

  const alerts = findAll(env.main, (node) => node.getAttribute('role') === 'alert');
  assert.ok(alerts.some((node) => text(node) === 'This preview changed.'));
});

test('a failed check after a failed upload shows its error once', async () => {
  let fail = false;
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': () =>
      fail ? new RequestError('Inky cannot be reached.', 'offline') : { preview: null },
    upload: () => {
      throw new RequestError('Upload failed.', 'offline');
    },
  });
  await renderImports(env.ui, env.helpers);
  const form = findAll(env.main, (node) => node.tagName === 'FORM')[0];
  const input = findAll(form, (node) => node.tagName === 'INPUT')[0];
  input.files = [{ size: 10 }];
  for (let attempt = 0; attempt < 2; attempt++) {
    form.fire('submit', {});
    await setImmediate();
  }
  const checks = findAll(form, (node) => node.tagName === 'BUTTON' && text(node) === 'Check saved upload');
  assert.equal(checks.length, 1);
  fail = true;
  await click(checks[0]);
  await settled(env.actions);

  const alerts = findAll(env.main, (node) => node.getAttribute('role') === 'alert');
  assert.ok(alerts.some((node) => text(node) === 'Inky cannot be reached.'));
});

test('the controller, not the browser clock, decides that a preview expired', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    // A tablet whose clock runs a day fast sees this as already past.
    '/v1/owner/imports': uploaded({ expires_at: new Date(Date.now() - 24 * HOUR).toISOString() }),
  });
  await renderImports(env.ui, env.helpers);

  assert.ok(findButton(env.main, 'Check this file'));
  assert.doesNotMatch(text(env.main), /Preview expired/);
});

test('an upload reports how much has been sent', async () => {
  let report;
  let finish;
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': { preview: null },
    upload: (onProgress) => {
      report = onProgress;
      return new Promise((resolve) => (finish = resolve));
    },
  });
  await renderImports(env.ui, env.helpers);
  const form = findAll(env.main, (node) => node.tagName === 'FORM')[0];
  const input = findAll(form, (node) => node.tagName === 'INPUT')[0];
  input.files = [{ size: 200 * 1024 * 1024 }];
  form.fire('submit', {});
  await setImmediate();
  assert.equal(typeof report, 'function');
  report(50 * 1024 * 1024, 200 * 1024 * 1024);
  const status = findAll(form, (node) => node.getAttribute('role') === 'status')[0];
  assert.match(text(status), /25%/);
  assert.match(text(status), /50 of 200 MiB/);
  finish({});
  await setImmediate();
});

function analyzerChecked(stats) {
  return uploaded({
    kind: 'birdnet-analyzer',
    status: 'checked',
    preview: {
      stats,
      impact: { live_source_enabled: false },
      generation_enabled: false,
      notice: 'Checked.',
      preview_hash: 'p',
      unchanged: false,
    },
  });
}

test('an Analyzer file that removes detections needs the review checkbox', async () => {
  const env = setup(
    {
      '/v1/owner/operations': { operations: [] },
      '/v1/owner/imports': analyzerChecked({ rows: 3, imported: 0, updated: 0, removed: 2 }),
      '/v1/owner/settings': { revision: 'r1' },
      '/v1/owner/imports/import-commit': { operation_id: 'op-2' },
      '/v1/owner/operations/op-2': { status: 'failed', result: { message: 'Stopped.' } },
    },
    { kind: 'birdnet-analyzer' },
  );
  await renderImports(env.ui, env.helpers);
  const box = findAll(env.main, (node) => node.tagName === 'INPUT' && node.type === 'checkbox')[0];
  assert.ok(box, 'review checkbox shown');
  assert.match(text(box.parent), /removed/i);
  await click(findButton(env.main, 'Confirm history update'));
  await settled(env.actions);
  assert.ok(!env.calls.some((call) => call.path === '/v1/owner/imports/import-commit'));
  box.checked = true;
  await click(findButton(env.main, 'Confirm history update'));
  await settled(env.actions);
  const commit = env.calls.find((call) => call.path === '/v1/owner/imports/import-commit');
  assert.equal(commit.body.allow_reduction, true);
});

test('an Analyzer file that removes nothing needs no checkbox', async () => {
  const env = setup(
    {
      '/v1/owner/operations': { operations: [] },
      '/v1/owner/imports': analyzerChecked({ rows: 3, imported: 3, updated: 0, removed: 0 }),
    },
    { kind: 'birdnet-analyzer' },
  );
  await renderImports(env.ui, env.helpers);
  assert.equal(
    findAll(env.main, (node) => node.tagName === 'INPUT' && node.type === 'checkbox').length,
    0,
  );
});

function tripState(preview) {
  return uploaded({
    status: 'committed',
    trip: {
      preview: {
        label: 'Spring trip',
        matched: 2,
        ready: 1,
        unresolved: [],
        notice: 'Save fixes this trip.',
        preview_hash: 'h',
        ...preview,
      },
    },
  });
}

test('a trip with names not looked up yet lists them and cannot be saved', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': tripState({
      unresolved_for_now: 1,
      not_looked_up: [{ common_name: 'Example Warbler', scientific_name: 'Exempla avis' }],
    }),
  });
  await renderImports(env.ui, env.helpers);

  assert.equal(findButton(env.main, 'Save this trip'), null);
  assert.match(text(env.main), /Exempla avis/);
  assert.ok(findButton(env.main, 'Edit trip selection'));
});

test('a fully looked-up trip can be saved', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': tripState({ unresolved_for_now: 0, not_looked_up: [] }),
  });
  await renderImports(env.ui, env.helpers);

  assert.ok(findButton(env.main, 'Save this trip'));
});

test('choosing checklists says unmatched names are looked up on iNaturalist', async () => {
  const env = setup({
    '/v1/owner/operations': { operations: [] },
    '/v1/owner/imports': uploaded({
      status: 'committed',
      preview: { stats: { earliest_observed_on: '2026-01-01', latest_observed_on: '2026-01-02' } },
    }),
  });
  await renderImports(env.ui, env.helpers);

  assert.match(text(env.main), /iNaturalist/);
});
