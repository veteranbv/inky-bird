import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const moduleURL = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
let source = await readFile(new URL('presentation.js', web), 'utf8');
for (const name of ['ui.js', 'frame-tools.js'])
  source = source.replace(
    `'/app/${name}'`,
    JSON.stringify(moduleURL(await readFile(new URL(name, web), 'utf8'))),
  );
const { renderPresentation } = await import(moduleURL(source));

const walk = (root, found = []) => {
  for (const child of root.children) {
    found.push(child);
    walk(child, found);
  }
  return found;
};
const alerts = (root) =>
  walk(root)
    .filter((node) => node.getAttribute('role') === 'alert' && node.textContent)
    .map((node) => node.textContent);
async function flush() {
  for (let n = 0; n < 10; n++) await setImmediate();
}
const failure = (message, code = 'operation_error') => Object.assign(new Error(message), { code });

const state = () => ({
  revision: 1,
  wall_view: 'current',
  dashboard_view: null,
  embed_allowed_origins: [],
  grants: [
    { id: 'g1', kind: 'screen', name: 'Hall tablet', view: 'current', expires_at: 2e9 },
    { id: 'g2', kind: 'status', name: 'Dashboard status', view: 'current', expires_at: 2e9 },
  ],
});

async function open(t, area, answer, extra = {}) {
  const names = ['document', 'location'];
  const saved = Object.fromEntries(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const doc = fakeDocument();
  doc.createTextNode = (text) => Object.assign(new Element('#text'), { text });
  const main = new Element('main');
  main.id = 'main';
  Object.defineProperty(main, 'lastChild', { get: () => main.children.at(-1) ?? null });
  doc.body.append(main);
  doc.querySelector = (selector) => (selector === '#main' ? main : null);
  const reloads = [];
  Object.defineProperty(globalThis, 'document', { configurable: true, writable: true, value: doc });
  Object.defineProperty(globalThis, 'location', {
    configurable: true,
    writable: true,
    value: { protocol: 'https:', origin: 'https://inky.example.test', reload: () => reloads.push(true) },
  });
  t.after(() => {
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
  });
  const calls = [],
    actions = [];
  const element = (tag, text, className) => {
    const node = new Element(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const ui = {
    element,
    button(text, action) {
      const node = element('button', text);
      node.type = 'button';
      node.addEventListener('click', (event) => actions.push(action({ currentTarget: node, ...event })));
      return node;
    },
    async api(path, body) {
      calls.push({ path, body });
      const value = await answer(path, body);
      if (value instanceof Error) throw value;
      return structuredClone(value);
    },
    showProblem() {},
    ...extra,
  };
  await renderPresentation(ui, {
    base(title) {
      main.replaceChildren(element('h1', title));
    },
    link: (text) => element('a', text),
    note: (text) => element('p', text, 'hint'),
    data: { collections: [], management: {} },
    area,
  });
  return { main, calls, reloads, actions };
}
const changes = (calls) => calls.filter(({ body }) => body);

test('presentation state that cannot load keeps the page and offers a retry', async (t) => {
  const { main, reloads } = await open(t, 'displays', () => failure('Displays could not be read.', 'journal_error'));
  assert.equal(main.children[0].textContent, 'Displays');
  assert.deepEqual(alerts(main), ['Displays could not be read.']);
  findButton(main, 'Try again').fire('click');
  assert.equal(reloads.length, 1);
});

test('revoking a screen asks first', async (t) => {
  const { main, calls, reloads } = await open(t, 'displays', (path, body) =>
    body ? {} : state(),
  );
  findButton(main, 'Revoke screen').fire('click');
  await flush();
  assert.equal(changes(calls).length, 0, 'the first press only asks');
  findButton(main, 'Keep access').fire('click');
  findButton(main, 'Revoke screen').fire('click');
  findButton(main, 'Revoke').fire('click');
  await flush();
  assert.deepEqual(changes(calls).map(({ body }) => [body.action, body.id]), [['revoke', 'g1']]);
  assert.equal(reloads.length, 1);
});

test('repeated failures in one place replace each other', async (t) => {
  const { main } = await open(t, 'displays', (path, body) =>
    body ? failure('The wall could not be changed.') : state(),
  );
  const wall = walk(main).find((node) => node.tagName === 'SECTION' && node.children[0]?.textContent === 'Physical wall panel');
  for (let n = 0; n < 3; n++) {
    findButton(wall, 'Confirm wall selection').fire('click');
    await flush();
  }
  assert.deepEqual(alerts(wall), ['Confirm the wall change first.']);
  walk(wall).find((node) => node.tagName === 'INPUT' && node.type === 'checkbox').checked = true;
  for (let n = 0; n < 3; n++) {
    findButton(wall, 'Confirm wall selection').fire('click');
    await flush();
  }
  assert.deepEqual(alerts(wall), ['The wall could not be changed.']);
});

test('a second confirmation request does not stack another panel', async (t) => {
  const { main } = await open(
    t,
    'displays',
    (path, body) => (body ? failure('Confirm it is you.', 'confirmation_required') : state()),
    { localPasswordAllowed: true },
  );
  const wall = walk(main).find((node) => node.tagName === 'SECTION' && node.children[0]?.textContent === 'Physical wall panel');
  walk(wall).find((node) => node.tagName === 'INPUT' && node.type === 'checkbox').checked = true;
  for (let n = 0; n < 2; n++) {
    findButton(wall, 'Confirm wall selection').fire('click');
    await flush();
  }
  assert.equal(walk(wall).filter((node) => node.textContent === 'Confirm and retry').length, 1);
});

test('a failed single sign-on confirmation is shown, not left unhandled', async (t) => {
  const { main, actions } = await open(
    t,
    'integrations',
    (path, body) => (body ? failure('Confirm it is you.', 'confirmation_required') : state()),
    {
      singleSignOnReady: true,
      confirmWithSSO: async () => {
        throw failure('The provider is unavailable.', 'access_error');
      },
    },
  );
  findButton(main, 'Turn dashboard art off')?.fire('click');
  findButton(main, 'Create status credential').fire('click');
  await flush();
  findButton(main, 'Confirm with single sign-on').fire('click');
  const results = await Promise.allSettled(actions);
  assert.ok(results.every((result) => result.status === 'fulfilled'), 'no rejected click');
  assert.ok(alerts(main).includes('The provider is unavailable.'));
});

test('a status credential is created once', async (t) => {
  let release;
  const { main, calls } = await open(t, 'integrations', (path, body) => {
    if (!body) return state();
    return new Promise((resolve) => {
      release = () => resolve({ secret: 'synthetic-secret' });
    });
  });
  const create = findButton(main, 'Create status credential');
  create.fire('click');
  await flush();
  assert.equal(create.disabled, true, 'the button waits for the answer');
  create.click();
  release();
  await flush();
  // A disabled control takes no press, as in a browser.
  create.click();
  await flush();
  assert.equal(changes(calls).length, 1, 'one credential is minted');
  assert.equal(create.disabled, true);
});

test('a failed status credential can be tried again', async (t) => {
  const { main, calls } = await open(t, 'integrations', (path, body) =>
    body ? failure('Could not create it.') : state(),
  );
  const create = findButton(main, 'Create status credential');
  create.fire('click');
  await flush();
  assert.equal(create.disabled, false);
  assert.deepEqual(alerts(main), ['Could not create it.']);
  assert.equal(changes(calls).length, 1);
});

test('a change pressed while another is saving says so', async (t) => {
  const busyState = state();
  busyState.grants.push({ id: 'g3', kind: 'screen', name: 'Den tablet', view: 'current', expires_at: 2e9 });
  const { main, calls } = await open(t, 'displays', (path, body) =>
    body ? new Promise(() => {}) : busyState,
  );
  const sections = walk(main).filter((node) => findButton(node, 'Revoke screen') && node.tagName === 'SECTION');
  const [first, second] = sections;
  findButton(first, 'Revoke screen').fire('click');
  findButton(first, 'Revoke').fire('click');
  await flush();
  findButton(second, 'Revoke screen').fire('click');
  findButton(second, 'Revoke').fire('click');
  await flush();
  assert.equal(changes(calls).length, 1, 'only one change is sent at a time');
  assert.deepEqual(alerts(second), ['Another change is still being saved. Try again when it finishes.']);
});
