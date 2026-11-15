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
  context.byId.set(7, { taxon_id: 7, common_name: 'Brown Creeper' });
  const section = collections.savedChange(context, 'Review adding to Saved birds', 'add', 7);
  main.append(section);
  findButton(section, 'Add to my frame').click();
  await settle(t);
  assert.deepEqual(assigned, []);
  assert.equal(server.changes.length, 1);
  assert.equal(server.changes[0].action, 'add');
  assert.equal(server.changes[0].taxon_id, 7);
  assert.equal(server.changes[0].confirm, true);
  assert.equal(server.changes[0].collection_revision, 'c'.repeat(64));
  assert(texts(section).includes('Brown Creeper will show on your frame from the next refresh.'));
  assert(findButton(section, 'Remove from my frame'));
  findButton(section, 'Undo').click();
  await settle(t);
  assert.equal(server.changes.length, 2);
  assert.equal(server.changes[1].action, 'remove');
  assert.notEqual(server.changes[1].key, server.changes[0].key);
  assert(texts(section).includes('Brown Creeper will leave your frame at the next refresh.'));
  assert(findButton(section, 'Add to my frame'));
  assert.equal(findButton(section, 'Undo'), null);
  assert.equal(texts(section).some((text) => /[0-9a-f]{32}/.test(text)), false);
});

test('a single-bird change that would touch other birds still asks first', async (t) => {
  const server = savedServer({ migrated_legacy_queue_count: 1 });
  const { context, main } = page(t, server.respond);
  const section = collections.savedChange(context, 'Review adding to Saved birds', 'add', 7);
  main.append(section);
  findButton(section, 'Add to my frame').click();
  await settle(t);
  assert.equal(server.changes.length, 0);
  assert(findButton(section, 'Change my frame'));
  assert(texts(section).includes('Adding 1 bird. Keeping 1 bird from your earlier setup.'));
  assert(!texts(section).some((text) => /legacy|seed|sighting|model/i.test(text)));
});

test('a failed toggle stays on the page with its message', async (t) => {
  const { context, main, requests } = page(t, (path) => {
    if (path === '/v1/owner/settings') return { revision: 'r' };
    throw new Error('Saved birds or approved artwork changed. Review this action again.');
  });
  const section = collections.savedToggle(context, { taxon_id: 7, saved: true });
  main.append(section);
  findButton(section, 'Remove from my frame').click();
  await settle(t);
  assert.equal(requests.at(-1).path, '/v1/owner/saved/preview');
  assert(texts(section).includes('Saved birds or approved artwork changed. Review this action again.'));
  assert.equal(findButton(section, 'Remove from my frame').disabled, false);
});

for (const [saved, shown, label] of [
  [false, 'Already on your frame.', 'Remove from my frame'],
  [true, 'Not on your frame.', 'Add to my frame'],
])
  test(`a stale ${saved ? 'remove' : 'add'} sends nothing and offers no Undo`, async (t) => {
    // Another tab already made this change, so the server reports nothing to do.
    const server = savedServer({ added_count: 0, removed_count: 0 });
    const { context, main } = page(t, server.respond);
    const section = collections.savedToggle(context, { taxon_id: 7, saved });
    main.append(section);
    findButton(section, saved ? 'Remove from my frame' : 'Add to my frame').click();
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
  findButton(section, 'Add to my frame').click();
  await settle(t);
  assert.equal(server.changes.length, 1);
  const shown = texts(section);
  assert(shown.includes(message));
  assert(!shown.some((text) => text.includes('will show on your frame')));
  assert(findButton(section, 'Add to my frame'));
  assert.equal(findButton(section, 'Undo'), null);
});

for (const [failing, hint] of [
  ['/v1/owner/saved/preview', false],
  ['/v1/owner/saved/change', true],
])
  test(`a lost connection at ${failing.split('/').pop()} ${hint ? 'says' : 'does not say'} to check On my frame`, async (t) => {
    // Only a change that was sent can have been applied without an answer.
    const server = savedServer();
    const { context, main } = page(t, (path, body) => {
      if (path === failing)
        throw Object.assign(new Error('Inky cannot be reached.'), { code: 'offline' });
      return server.respond(path, body);
    });
    const section = collections.savedToggle(context, { taxon_id: 7, saved: false });
    main.append(section);
    findButton(section, 'Add to my frame').click();
    await settle(t);
    const hinted = 'Inky cannot be reached. Check On my frame to see whether it changed.';
    assert(texts(section).includes(hint ? hinted : 'Inky cannot be reached.'));
    assert.equal(texts(section).includes(hinted), hint);
  });

test('changes to many birds keep their review step', async (t) => {
  const server = savedServer({ added_count: 3 });
  const { context, main } = page(t, server.respond);
  const section = collections.savedChange(
    context,
    'Add every bird with a plate',
    'import-approved',
  );
  main.append(section);
  findButton(section, 'Add every bird with a plate').click();
  await settle(t);
  assert.equal(server.changes.length, 0);
  assert(texts(section).includes('Adding 3 birds.'));
});

test('an outcome shows plain words, never identifiers or hashes', async () => {
  assert.equal(manage.statusText('succeeded'), 'Done.');
  assert.equal(manage.statusText('something-new'), 'Inky could not tell how this went.');
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
    ['1 bird added', '2 birds removed', '0 birds kept from your earlier setup', 'Plate making is off'],
  );
  // Every other field is shown: in words where this page has them, otherwise
  // under a readable label, never as a raw key.
  assert.deepEqual(
    manage.resultLines({
      window: 'last-30-days',
      radius_km: 25,
      earliest_observed_on: '2001-03-01',
      latest_observed_on: '2001-03-09',
      rows: 3,
      added_checklists: 1,
      dry_run: true,
    }),
    [
      'Window: Last 30 days',
      'Radius km: 25',
      'Earliest checklist: 1 Mar 2001',
      'Latest checklist: 9 Mar 2001',
      'Rows in the file: 3',
      'New checklists: 1',
      'Nothing was changed: this was a trial run',
    ],
  );
});

test('queued plate requests show every count the controller returns', () => {
  // The queue-plates result from controller.queue_owner_selection_locked.
  assert.deepEqual(
    manage.resultLines({ message: 'x', queued: 3, already_queued: 1, held: 2, ready: 0 }),
    [
      '3 plates asked for',
      '1 bird already waiting for a plate',
      '2 birds left as they are',
      '0 birds already with a plate',
    ],
  );
});

test('the outcome page says what happened to the frame, without raw keys', async (t) => {
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
  assert.equal(main.querySelector('h1').textContent, 'Change to your frame');
  assert(shown.includes('Your frame was changed. It updates at its next refresh.'));
  assert(!shown.some((text) => /Operation|Owner operation|Finished/.test(text)));
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
  const setup = [];
  (function walk(node) {
    for (const child of node.children) {
      if (child.tagName === 'A' && child.textContent === 'Set up') setup.push(child);
      walk(child);
    }
  })(main);
  assert.deepEqual(setup.map((node) => node.getAttribute('aria-label')), ['Set up iNaturalist']);
  assert(shown.includes('Birds noted in the last 30 days. Inky checks each source on a schedule; it does not listen all the time.'));
  assert(!shown.some((text) => text.includes('last-30-days')));
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
  assert(shown.includes('No birds need trying again.'));
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
  const queue = findButton(main, 'Try at the next check');
  assert.equal(queue.disabled, true);
  assert(texts(main).includes('Select between 1 and 32 birds.'));
  const box = main.querySelector('input');
  box.checked = true;
  box.fire('change');
  assert.equal(queue.disabled, false);
  assert(!texts(main).includes('Select between 1 and 32 birds.'));
  assert.equal(main.querySelector('h1').textContent, 'In the works');
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

// Saved birds as the controller keeps them: adding needs approved art.
function membershipServer(members, approved) {
  const changes = [];
  const receipts = new Map();
  const preview = (body) => {
    if (body.action === 'add' && !approved.has(body.taxon_id))
      throw new Error('Choose approved artwork before adding it to Saved birds');
    const has = members.has(body.taxon_id);
    return {
      added_count: body.action === 'add' && !has ? 1 : 0,
      removed_count: body.action === 'remove' && has ? 1 : 0,
      migrated_legacy_queue_count: 0,
      collection_revision: 'c'.repeat(64),
    };
  };
  return {
    changes,
    respond(path, body) {
      if (path === '/v1/owner/settings') return { revision: 'r'.repeat(64) };
      if (path === '/v1/owner/saved/preview') return preview(body);
      if (path === '/v1/owner/saved/change') {
        changes.push(body);
        let result;
        try {
          result = preview(body);
          if (body.action === 'add') members.add(body.taxon_id);
          else members.delete(body.taxon_id);
        } catch (error) {
          result = { message: error.message };
        }
        const id = 'op' + changes.length;
        receipts.set(id, { result, status: result.message ? 'failed' : 'succeeded' });
        return { operation_id: id, kind: 'saved-collection', status: 'pending' };
      }
      if (path.startsWith('/v1/owner/operations/')) {
        const id = path.split('/').pop();
        return { operation_id: id, kind: 'saved-collection', ...receipts.get(id) };
      }
      throw new Error('unexpected ' + path);
    },
  };
}

for (const [start, first] of [
  [new Set(), 'Add to my frame'],
  [new Set([7, 8]), 'Remove from my frame'],
])
  test(`Undo after one tap to ${first.split(' ')[0].toLowerCase()} restores On my frame`, async (t) => {
    const members = new Set(start);
    const server = membershipServer(members, new Set([7]));
    const { context, main, assigned } = page(t, server.respond);
    const section = collections.savedChange(
      context,
      '',
      start.has(7) ? 'remove' : 'add',
      7,
      true,
    );
    main.append(section);
    findButton(section, first).click();
    await settle(t);
    assert.notDeepEqual([...members].sort(), [...start].sort());
    findButton(section, 'Undo').click();
    await settle(t);
    assert.deepEqual([...members].sort(), [...start].sort());
    assert.equal(server.changes.length, 2);
    assert.equal(findButton(section, first) !== null, true);
    assert.deepEqual(assigned, []);
  });

test('removing a bird without approved art asks first and offers no Undo', async (t) => {
  const members = new Set([7]);
  const server = membershipServer(members, new Set());
  const { context, main, assigned } = page(t, server.respond);
  const section = collections.savedChange(context, '', 'remove', 7, false);
  main.append(section);
  findButton(section, 'Remove from my frame').click();
  await settle(t);
  // Nothing changed yet: the owner is asked, since adding it back would be refused.
  assert.equal(server.changes.length, 0);
  assert.deepEqual([...members], [7]);
  assert.equal(findButton(section, 'Undo'), null);
  assert(
    texts(section).includes(
      'This bird has no plate yet, so you can add it back only once it has one.',
    ),
  );
  const confirm = findButton(section, 'Change my frame');
  confirm.fire('click', { currentTarget: confirm });
  await settle(t);
  assert.deepEqual([...members], []);
  assert.deepEqual(assigned, ['/app?page=operation&operation=op1']);
});

test('a bird page with approved art removes it in one tap', async (t) => {
  const members = new Set([7]);
  const server = membershipServer(members, new Set([7]));
  const { context, main } = page(t, server.respond);
  const section = collections.savedChange(context, '', 'remove', 7, true);
  main.append(section);
  findButton(section, 'Remove from my frame').click();
  await settle(t);
  assert.deepEqual([...members], []);
  assert(texts(section).includes('This bird will leave your frame at the next refresh.'));
  assert(findButton(section, 'Undo'));
});

test('an interrupted change says to check On my frame', async (t) => {
  const message = 'Interrupted. The outcome is not confirmed. Inspect state before an explicit retry.';
  const server = savedServer({}, 'interrupted', { message });
  const { context, main } = page(t, server.respond);
  const section = collections.savedToggle(context, { taxon_id: 7, saved: false });
  main.append(section);
  findButton(section, 'Add to my frame').click();
  await settle(t);
  assert(texts(section).includes(message + ' Check On my frame to see whether it changed.'));
});

const candidatePage = await import(moduleURL(await inline("export * from '/app/candidate.js';")));

test('a refused rejection is sent again with current settings and reason', async (t) => {
  let revision = 'old';
  const decisions = [];
  const { context, main } = page(t, (path, body) => {
    if (path.startsWith('/v1/owner/candidates/7/')) {
      decisions.push(body);
      if (decisions.length === 1)
        throw Object.assign(new Error('Settings changed. Reload and review again.'), {
          code: 'journal_changed',
        });
      if (decisions.length === 2)
        throw Object.assign(new Error('Inky cannot be reached.'), { code: 'offline' });
      return { operation_id: 'op1' };
    }
    if (path.startsWith('/v1/owner/candidates/')) {
      return {
        taxon_id: 7,
        common_name: 'Example Wren',
        scientific_name: 'Exempli wren',
        notice: '',
        portrait_url: '/candidate.png',
        quality_review: {},
        verification_sources: [],
        candidate_revision: 'c'.repeat(64),
        can_approve: false,
      };
    }
    if (path === '/v1/owner/settings') return { revision };
    throw new Error('unexpected ' + path);
  });
  context.query = new URLSearchParams({ bird: '7' });
  await candidatePage.renderCandidate(context);
  const reason = main.querySelector('textarea');
  reason.value = 'First reason';
  const reject = findButton(main, 'Confirm rejection');
  reject.fire('click', { currentTarget: reject });
  await flush();
  assert(texts(main).includes('Settings changed. Reload and review again.'));
  revision = 'new';
  reason.value = 'Second reason';
  reject.fire('click', { currentTarget: reject });
  await flush();
  // A lost answer keeps the same request, so a retry cannot decide twice.
  reject.fire('click', { currentTarget: reject });
  await flush();
  assert.equal(decisions.length, 3);
  assert.equal(decisions[0].configuration_revision, 'old');
  assert.equal(decisions[1].configuration_revision, 'new');
  assert.equal(decisions[1].reason, 'Second reason');
  assert.notEqual(decisions[1].key, decisions[0].key);
  assert.deepEqual(decisions[2], decisions[1]);
});

test('a community collection that cannot be saved keeps its preview on the page', async (t) => {
  const { context, main, problems } = page(t, (path) => {
    if (path === '/v1/owner/community')
      return {
        status: 'ready',
        preview_hash: 'p'.repeat(64),
        notice: 'Community reports are not your sightings.',
        collection: {
          label: 'Synthetic search',
          start_date: '2001-03-01',
          end_date: '2001-03-02',
          members: [{ common_name: 'Example Wren', scientific_name: 'Exempli wren' }],
        },
      };
    if (path === '/v1/owner/community/save')
      throw Object.assign(new Error('This community preview changed or expired. Search again.'), {
        code: 'journal_changed',
      });
    throw new Error('unexpected ' + path);
  });
  await collections.renderCommunity(context);
  findButton(main, 'Save as a collection').click();
  await flush();
  assert.deepEqual(problems, []);
  const shown = texts(main);
  assert(shown.includes('This community preview changed or expired. Search again.'));
  assert(shown.includes('Example Wren · Exempli wren'));
});
