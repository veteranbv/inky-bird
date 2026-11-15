import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const moduleURL = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
// A data: module cannot resolve '/app/' paths, so its imports are inlined too.
const inlined = new Map();
async function inline(source) {
  for (const [, path] of source.matchAll(/from '(\/app\/[\w-]+\.js)'/g)) {
    if (!inlined.has(path))
      inlined.set(path, moduleURL(await inline(await readFile(new URL(path.slice(5), web), 'utf8'))));
    source = source.replace(`'${path}'`, JSON.stringify(inlined.get(path)));
  }
  return source;
}
const manage = await import(moduleURL(await inline("export * from '/app/manage.js';")));
const collections = await import(moduleURL(await inline("export * from '/app/collections.js';")));

async function flush() {
  for (let n = 0; n < 20; n++) await setImmediate();
}
function texts(node, found = []) {
  if (!node.children.length && node.text) found.push(node.text);
  for (const child of node.children) texts(child, found);
  return found;
}

// A page with the journal's shared helpers. `respond(path, body)` answers the API.
function page(t, respond) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const saved = Object.fromEntries(
    ['document', 'location'].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const doc = fakeDocument();
  doc.createTextNode = (text) => {
    const node = new Element('#text');
    node.text = String(text);
    return node;
  };
  const main = new Element('main');
  doc.body.append(main);
  const assigned = [];
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  define('document', doc);
  define('location', { assign: (url) => assigned.push(url), reload() {} });
  t.after(() => {
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
  });
  const requests = [];
  const el = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, action) => {
    const node = el('button', text, 'secondary');
    node.addEventListener('click', action);
    return node;
  };
  const href = (name, extra = {}) => '/app?' + new URLSearchParams({ page: name, ...extra });
  const link = (text, name, extra = {}) => {
    const node = el('a', text, 'text-link');
    node.href = href(name, extra);
    return node;
  };
  const problems = [];
  const context = {
    ui: { showProblem: (error) => problems.push(error) },
    el,
    button,
    api: async (path, body) => {
      requests.push({ path, body });
      return respond(path, body);
    },
    main,
    href,
    link,
    note: (text) => el('p', text, 'hint'),
    base: (title) => main.replaceChildren(el('h1', title)),
    byId: new Map(),
    sourceNames: { inaturalist: ['iNaturalist', 'Community reports'] },
    operationNames: {},
    setStopScreen() {},
  };
  return { context, main, requests, assigned, problems };
}

function savedServer(preview = {}, finished = 'succeeded', result = {}) {
  const changes = [];
  let polls = 0;
  return {
    changes,
    respond(path, body) {
      if (path === '/v1/owner/settings') return { revision: 'r'.repeat(64) };
      if (path === '/v1/owner/saved/preview')
        return {
          added_count: body.action === 'add' ? 1 : 0,
          removed_count: body.action === 'remove' ? 1 : 0,
          migrated_legacy_queue_count: 0,
          collection_revision: 'c'.repeat(64),
          ...preview,
        };
      if (path === '/v1/owner/saved/change') {
        changes.push(body);
        return { operation_id: 'op' + changes.length, kind: 'saved-collection', status: 'pending' };
      }
      if (path.startsWith('/v1/owner/operations/')) {
        polls += 1;
        return {
          operation_id: path.split('/').pop(),
          kind: 'saved-collection',
          status: polls % 2 ? 'running' : finished,
          result: {
            added_count: 1,
            removed_count: 0,
            collection_revision: 'c'.repeat(64),
            ...result,
          },
        };
      }
      throw new Error('unexpected ' + path);
    },
  };
}

async function settle(t) {
  for (let n = 0; n < 6; n++) {
    await flush();
    t.mock.timers.tick(1000);
  }
  await flush();
}

test('one bird is saved with one tap, an inline result and Undo', async (t) => {
  const server = savedServer();
  const { context, main, assigned } = page(t, server.respond);
  const section = collections.savedChange(context, 'Review adding to Saved birds', 'add', 7);
  main.append(section);
  findButton(section, 'Add to Saved birds').click();
  await settle(t);
  assert.deepEqual(assigned, []);
  assert.equal(server.changes.length, 1);
  assert.equal(server.changes[0].action, 'add');
  assert.equal(server.changes[0].taxon_id, 7);
  assert.equal(server.changes[0].confirm, true);
  assert.equal(server.changes[0].collection_revision, 'c'.repeat(64));
  assert(texts(section).includes('Added to Saved birds.'));
  assert(findButton(section, 'Remove from Saved birds'));
  findButton(section, 'Undo').click();
  await settle(t);
  assert.equal(server.changes.length, 2);
  assert.equal(server.changes[1].action, 'remove');
  assert.notEqual(server.changes[1].key, server.changes[0].key);
  assert(texts(section).includes('Removed from Saved birds.'));
  assert(findButton(section, 'Add to Saved birds'));
  assert.equal(findButton(section, 'Undo'), null);
  assert.equal(texts(section).some((text) => /[0-9a-f]{32}/.test(text)), false);
});

test('a single-bird change that would touch other birds still asks first', async (t) => {
  const server = savedServer({ migrated_legacy_queue_count: 1 });
  const { context, main } = page(t, server.respond);
  const section = collections.savedChange(context, 'Review adding to Saved birds', 'add', 7);
  main.append(section);
  findButton(section, 'Add to Saved birds').click();
  await settle(t);
  assert.equal(server.changes.length, 0);
  assert(findButton(section, 'Confirm Saved birds change'));
  assert(texts(section).includes('1 addition · 0 removals · 1 legacy seed member migrated'));
});

test('a failed toggle stays on the page with its message', async (t) => {
  const { context, main, requests } = page(t, (path) => {
    if (path === '/v1/owner/settings') return { revision: 'r' };
    throw new Error('Saved birds or approved artwork changed. Review this action again.');
  });
  const section = collections.savedToggle(context, { taxon_id: 7, saved: true });
  main.append(section);
  findButton(section, 'Remove from Saved birds').click();
  await settle(t);
  assert.equal(requests.at(-1).path, '/v1/owner/saved/preview');
  assert(texts(section).includes('Saved birds or approved artwork changed. Review this action again.'));
  assert.equal(findButton(section, 'Remove from Saved birds').disabled, false);
});

for (const [saved, shown, label] of [
  [false, 'Already in Saved birds.', 'Remove from Saved birds'],
  [true, 'Not in Saved birds.', 'Add to Saved birds'],
])
  test(`a stale ${saved ? 'remove' : 'add'} sends nothing and offers no Undo`, async (t) => {
    // Another tab already made this change, so the server reports nothing to do.
    const server = savedServer({ added_count: 0, removed_count: 0 });
    const { context, main } = page(t, server.respond);
    const section = collections.savedToggle(context, { taxon_id: 7, saved });
    main.append(section);
    findButton(section, saved ? 'Remove from Saved birds' : 'Add to Saved birds').click();
    await settle(t);
    assert.equal(server.changes.length, 0);
    assert(texts(section).includes(shown));
    assert(findButton(section, label));
    assert.equal(findButton(section, 'Undo'), null);
  });

test('a change the controller refused does not flip the toggle', async (t) => {
  const message = 'Saved birds or approved artwork changed. Review this action again.';
  const server = savedServer({}, 'failed', { message });
  const { context, main } = page(t, server.respond);
  const section = collections.savedToggle(context, { taxon_id: 7, saved: false });
  main.append(section);
  findButton(section, 'Add to Saved birds').click();
  await settle(t);
  assert.equal(server.changes.length, 1);
  const shown = texts(section);
  assert(shown.includes(message));
  assert(!shown.includes('Added to Saved birds.'));
  assert(findButton(section, 'Add to Saved birds'));
  assert.equal(findButton(section, 'Undo'), null);
});

for (const [failing, hint] of [
  ['/v1/owner/saved/preview', false],
  ['/v1/owner/saved/change', true],
])
  test(`a lost connection at ${failing.split('/').pop()} ${hint ? 'says' : 'does not say'} to check Saved birds`, async (t) => {
    // Only a change that was sent can have been applied without an answer.
    const server = savedServer();
    const { context, main } = page(t, (path, body) => {
      if (path === failing)
        throw Object.assign(new Error('Inky cannot be reached.'), { code: 'offline' });
      return server.respond(path, body);
    });
    const section = collections.savedToggle(context, { taxon_id: 7, saved: false });
    main.append(section);
    findButton(section, 'Add to Saved birds').click();
    await settle(t);
    const hinted = 'Inky cannot be reached. Check Saved birds if the connection was lost.';
    assert(texts(section).includes(hint ? hinted : 'Inky cannot be reached.'));
    assert.equal(texts(section).includes(hinted), hint);
  });

test('bulk Saved birds changes keep their review step', async (t) => {
  const server = savedServer({ added_count: 3 });
  const { context, main } = page(t, server.respond);
  const section = collections.savedChange(
    context,
    'Review adding all available approved art',
    'import-approved',
  );
  main.append(section);
  findButton(section, 'Review adding all available approved art').click();
  await settle(t);
  assert.equal(server.changes.length, 0);
  assert(texts(section).includes('3 additions · 0 removals · 0 legacy seed members migrated'));
});

test('an outcome shows plain words, never identifiers or hashes', async () => {
  assert.equal(manage.statusText('succeeded'), 'Finished');
  assert.equal(manage.statusText('something-new'), 'Status unknown');
  assert.deepEqual(
    manage.resultLines({
      added_count: 1,
      removed_count: 2,
      migrated_legacy_queue_count: 0,
      collection_revision: 'a'.repeat(64),
      preview_hash: 'b'.repeat(64),
      upload_id: 'c'.repeat(32),
      request_ids: ['x'],
      revision: 4,
      dry_run: false,
      error_type: 'JournalError',
      generation_enabled: false,
    }),
    ['1 bird added', '2 birds removed', '0 legacy seed members migrated', 'Generation off'],
  );
  // Fields this page has no words for are left out, never shown by their key.
  assert.deepEqual(
    manage.resultLines({
      window: 'last-30-days',
      start_date: '2001-03-01',
      radius_km: 25,
      species_limit: 10,
      earliest_observed_on: '2001-03-01',
      rows: 3,
      added_checklists: 1,
    }),
    ['Source rows: 3', 'New checklists: 1'],
  );
});

test('the outcome page renders a Saved birds receipt without raw keys', async (t) => {
  const { context, main } = page(t, () => ({
    operation_id: 'op1',
    kind: 'saved-collection',
    status: 'succeeded',
    result: {
      added_count: 1,
      removed_count: 0,
      migrated_legacy_queue_count: 0,
      collection_revision: '71bc32' + 'a'.repeat(58),
    },
  }));
  context.query = new URLSearchParams({ operation: 'op1' });
  await manage.renderOperation(context);
  const shown = texts(main);
  assert(shown.includes('Change Saved birds'));
  assert(shown.includes('Finished'));
  assert(shown.includes('1 bird added'));
  assert(!shown.some((text) => text.includes('succeeded') || text.includes('71bc32')));
  assert(!shown.some((text) => text.includes('_') || text.includes('count:')));
});

test('a source this page does not know still shows the Sources page', async (t) => {
  const source = (name) => ({ name, enabled: true, status: 'ok', activity: {} });
  const management = {
    available: true,
    window: 'last-30-days',
    refresh_minutes: 15,
    sources: [source('inaturalist'), source('newer-source')],
    work: { complete: true, discovery_status: 'ok' },
    activity: { entries: {} },
    recent_detections: [],
  };
  const { context, main } = page(t, (path) => {
    if (path === '/v1/owner/operations') return { operations: [] };
    throw new Error('unexpected ' + path);
  });
  Object.assign(context, {
    area: 'sources',
    data: {},
    management,
    unreadable: () => new Element('div'),
    empty: () => new Element('section'),
    operationAction: () => new Element('section'),
    currentRun: () => 0,
  });
  await manage.renderStatus(context);
  const shown = texts(main);
  assert(shown.includes('newer-source'));
  assert(shown.includes('Set up iNaturalist'));
  assert(!shown.includes('Set up newer-source'));
});

test('a source that first appears in a status update does not stop the updates', async (t) => {
  const source = (name) => ({ name, enabled: true, status: 'ok', activity: {} });
  const management = {
    available: true,
    window: 'last-30-days',
    refresh_minutes: 15,
    sources: [source('inaturalist')],
    work: { complete: true, discovery_status: 'ok' },
    activity: { entries: {} },
    recent_detections: [],
  };
  const { context, main } = page(t, (path) => {
    if (path === '/v1/owner/operations') return { operations: [] };
    if (path === '/v1/owner/management')
      return { ...management, sources: [source('inaturalist'), source('newer-source')] };
    throw new Error('unexpected ' + path);
  });
  Object.assign(context, {
    area: 'sources',
    data: {},
    management,
    unreadable: () => new Element('div'),
    empty: () => new Element('section'),
    operationAction: () => new Element('section'),
    currentRun: () => 0,
  });
  await manage.renderStatus(context);
  t.mock.timers.tick(5000);
  await flush();
  assert(texts(main).some((text) => text.startsWith('Updated ')));
});

test('no retry hint shows while there are no birds to choose', async (t) => {
  const action = () => {
    const section = new Element('section');
    section.append(new Element('button'));
    return section;
  };
  const management = {
    available: true,
    generation_enabled: false,
    retry_wake_available: false,
    activity: { entries: {} },
    work: {
      complete: true,
      queue: [],
      can_run: [],
      waiting: [],
      needs_attention: [],
      retry_details: [],
      terminal_details: [],
      in_review: { status: 'ok', candidates: [] },
      retry_requests: [],
    },
  };
  const { context, main } = page(t, (path) => {
    if (path === '/v1/owner/operations') return { operations: [] };
    throw new Error('unexpected ' + path);
  });
  Object.assign(context, {
    area: 'work',
    query: new URLSearchParams(),
    data: {},
    management,
    unreadable: () => new Element('div'),
    empty: () => new Element('section'),
    operationAction: action,
    currentRun: () => 0,
  });
  await manage.renderStatus(context);
  const shown = texts(main);
  assert(shown.includes('No birds currently need retry selection.'));
  assert(!shown.includes('Select between 1 and 32 birds.'));
});

test('a retry selection outside 1 to 32 is a note beside the choices', async (t) => {
  const action = (label) => {
    const section = new Element('section');
    const start = new Element('button');
    start.textContent = label;
    section.append(start);
    return section;
  };
  const management = {
    available: true,
    generation_enabled: false,
    retry_wake_available: false,
    activity: { entries: {} },
    work: {
      complete: true,
      queue: [],
      can_run: [5],
      waiting: [],
      needs_attention: [],
      retry_details: [],
      terminal_details: [],
      in_review: { status: 'ok', candidates: [] },
      retry_requests: [],
    },
  };
  const { context, main } = page(t, (path) => {
    if (path === '/v1/owner/operations') return { operations: [] };
    throw new Error('unexpected ' + path);
  });
  Object.assign(context, {
    area: 'work',
    query: new URLSearchParams(),
    data: {},
    management,
    unreadable: () => new Element('div'),
    empty: () => new Element('section'),
    operationAction: action,
    currentRun: () => 0,
  });
  context.byId.set(5, { taxon_id: 5, common_name: 'Example Wren', plate_ready: false });
  await manage.renderStatus(context);
  const queue = findButton(main, 'Queue selected for the next scheduled check');
  assert.equal(queue.disabled, true);
  assert(texts(main).includes('Select between 1 and 32 birds.'));
  const box = main.querySelector('input');
  box.checked = true;
  box.fire('change');
  assert.equal(queue.disabled, false);
  assert(!texts(main).includes('Select between 1 and 32 birds.'));
  assert.equal(main.querySelector('h1').textContent, 'Plate work');
});

test('the editor keeps unresolved names unless one is unticked', async (t) => {
  const group = {
    collection_id: 'a'.repeat(32),
    label: 'A trip',
    kind: 'personal-trip',
    taxon_ids: [],
    unresolved: [
      { common_name: 'Robin', scientific_name: 'Turdus migratorius' },
      { common_name: '', scientific_name: 'Unmatched species' },
    ],
  };
  const { context, main, requests } = page(t, () => ({}));
  Object.assign(context, {
    query: new URLSearchParams({ view: group.collection_id }),
    data: {
      revision: 3,
      collections: [group],
      birds: [
        { taxon_id: 1, common_name: 'American Robin', scientific_name: 'Turdus migratorius', plate_ready: true },
      ],
    },
  });
  collections.renderEditCollection(context);
  const form = main.querySelector('form');
  form.fire('submit');
  await flush();
  assert.equal('remove_unresolved' in requests.at(-1).body, false);
  assert(texts(main).includes('Turdus migratorius · matches American Robin'));
  const boxes = [];
  (function walk(node) {
    for (const child of node.children) {
      if (child.tagName === 'INPUT' && child.type === 'checkbox') boxes.push(child);
      walk(child);
    }
  })(form);
  // The bird first, then the two unresolved names.
  boxes[0].checked = true;
  boxes[0].fire('change');
  boxes[1].checked = false;
  boxes[1].fire('change');
  form.fire('submit');
  await flush();
  assert.deepEqual(requests.at(-1).body, {
    revision: 3,
    label: 'A trip',
    taxon_ids: [1],
    collection_id: group.collection_id,
    remove_unresolved: [{ common_name: 'Robin', scientific_name: 'Turdus migratorius' }],
  });
});
