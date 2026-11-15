import assert from 'node:assert/strict';
import test from 'node:test';
import { Element, findButton } from './fake-dom.mjs';
import { bird, flush, images, journal, nodes, open, sha, withState } from './journal-harness.mjs';

const byId = (main, id) => nodes(main).find((node) => node.id === id);
const links = (root) => nodes(root).filter((node) => node.tagName === 'A');
const linkNamed = (root, text) => links(root).find((node) => node.textContent === text);
const params = (href) => Object.fromEntries(new URL(href, 'https://inky.example.test').searchParams);
const grid = (main) => main.querySelector('.bird-grid');
const names = (main) =>
  grid(main)
    .children.filter((node) => node.classList.contains('bird-card') && !node.hidden)
    .map((node) => node.children.find((child) => child.tagName === 'H2').textContent);
const count = (main) => nodes(main).find((node) => node.id === 'bird-count').textContent;

// Four birds whose name, scientific name and last record orders all differ.
function flock() {
  const state = journal();
  state.birds = [
    { ...bird(1, 'Example Wren', 'a'), scientific_name: 'Zeta wren' },
    {
      ...bird(2, 'Sample Finch', 'b'),
      scientific_name: 'Alpha finch',
      retained_evidence: [{ source: 'ebird-archive', count: 1, unit: 'checklists', last_noted: '2001-03-02' }],
    },
    {
      ...bird(3, 'Test Heron', 'c'),
      scientific_name: 'Mu heron',
      retained_evidence: [{ source: 'ebird-archive', count: 2, unit: 'checklists', last_noted: '2001-05-09' }],
    },
    { ...bird(4, 'Demo Plover', 'd'), plate_ready: false, portrait_url: null, portrait_sha256: undefined },
  ];
  state.active_taxon_ids = [1, 3];
  state.collections = [
    { collection_id: 'fav', kind: 'favorites', label: 'Favorites', taxon_ids: [3, 2] },
  ];
  return state;
}

test('the search term comes from the address and is written back without a new entry', async (t) => {
  const { main, history } = await open(t, { page: 'birds', q: 'finch' }, { state: flock });
  assert.equal(byId(main, 'bird-search').value, 'finch');
  assert.deepEqual(names(main), ['Sample Finch']);
  assert.equal(count(main), '1 species');
  const search = byId(main, 'bird-search');
  search.value = 'heron';
  search.fire('input');
  assert.deepEqual(names(main), ['Test Heron']);
  assert.deepEqual(params(history.entries.at(-1)), { page: 'birds', q: 'heron' });
  search.value = '';
  search.fire('input');
  assert.deepEqual(params(history.entries.at(-1)), { page: 'birds' });
});

test('sort reorders the same cards and filters hide the rest', async (t) => {
  const { main, history } = await open(t, { page: 'birds' }, { state: flock });
  const before = images(grid(main));
  assert.deepEqual(names(main), ['Demo Plover', 'Example Wren', 'Sample Finch', 'Test Heron']);
  const sort = byId(main, 'bird-sort');
  sort.value = 'scientific';
  sort.fire('change');
  assert.deepEqual(names(main), ['Sample Finch', 'Demo Plover', 'Test Heron', 'Example Wren']);
  sort.value = 'recent';
  sort.fire('change');
  assert.deepEqual(names(main), ['Test Heron', 'Sample Finch', 'Demo Plover', 'Example Wren']);
  assert.deepEqual(params(history.entries.at(-1)), { page: 'birds', sort: 'recent' });
  const show = byId(main, 'bird-show');
  for (const [value, expected] of [
    ['favorites', ['Test Heron', 'Sample Finch']],
    ['current', ['Test Heron', 'Example Wren']],
    ['record', ['Test Heron', 'Sample Finch']],
    ['missing', ['Demo Plover']],
  ]) {
    show.value = value;
    show.fire('change');
    assert.deepEqual(names(main), expected, value);
  }
  assert.equal(count(main), '1 species');
  assert.deepEqual(params(history.entries.at(-1)), { page: 'birds', sort: 'recent', show: 'missing' });
  const after = images(grid(main));
  assert.equal(after.length, before.length);
  assert(after.every((image) => before.includes(image)), 'no image is recreated');
});

test('an unknown sort or filter in the address falls back to the full list by name', async (t) => {
  const { main } = await open(t, { page: 'birds', sort: 'constructor', show: 'toString' }, { state: flock });
  assert.equal(byId(main, 'bird-sort').value, 'name');
  assert.equal(byId(main, 'bird-show').value, 'all');
  assert.equal(names(main).length, 4);
});

test('cards keep the list in their link and remember the card for Back', async (t) => {
  const { main, history } = await open(
    t,
    { page: 'birds', scope: 'art', q: 'e', sort: 'scientific' },
    { state: flock, historyState: { kept: true } },
  );
  const card = grid(main).children.find((node) => node.textContent.includes('Test Heron'));
  assert.deepEqual(params(card.href), {
    page: 'bird',
    bird: '3',
    scope: 'art',
    q: 'e',
    sort: 'scientific',
  });
  card.fire('click');
  assert.deepEqual(history.state, { kept: true, birdsAt: 3 });
});

for (const [name, query, historyState] of [
  ['Back', { page: 'birds' }, { birdsAt: 3 }],
  ['the bird page link', { page: 'birds', at: '3' }, null],
])
  test('returning through ' + name + ' brings the same card into view', async (t) => {
    const { doc, main, history } = await open(t, query, { state: flock, historyState });
    const card = grid(main).children.find((node) => node.textContent.includes('Test Heron'));
    assert.deepEqual(card.scrolledIntoView, { block: 'center' });
    assert.equal(doc.activeElement, card);
    assert.equal(history.scrollRestoration, 'manual');
    assert.equal(params(location.href).at, undefined, 'the one-time position leaves the address');
    assert.equal(history.state?.birdsAt, undefined);
  });

test('the bird page starts with Back and steps through the same list', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '3', sort: 'scientific', show: 'favorites' }, { state: flock });
  const top = main.children[0];
  assert(top.classList.contains('bird-nav'), 'the bird links come before the title');
  assert.equal(main.children[1].tagName, 'H1');
  const back = linkNamed(top, 'Back to birds');
  assert.deepEqual(params(back.href), { page: 'birds', sort: 'scientific', show: 'favorites', at: '3' });
  assert.equal(links(main).filter((node) => node.textContent === 'Back to birds').length, 1);
  const previous = linkNamed(top, 'Previous');
  assert.deepEqual(params(previous.href), { page: 'bird', bird: '2', sort: 'scientific', show: 'favorites' });
  assert.equal(previous.getAttribute('aria-label'), 'Previous: Sample Finch');
  assert.equal(linkNamed(top, 'Next'), undefined, 'the last bird has no next');
});

test('previous and next follow the chosen sort, not the stored order', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '2', sort: 'recent' }, { state: flock });
  assert.equal(params(linkNamed(main, 'Previous').href).bird, '3');
  assert.equal(params(linkNamed(main, 'Next').href).bird, '4');
});

test('opening a card in a new tab leaves this list where it is', async (t) => {
  const { main, history } = await open(t, { page: 'birds' }, { state: flock, historyState: { kept: true } });
  const card = grid(main).children.find((node) => node.textContent.includes('Test Heron'));
  for (const key of ['metaKey', 'ctrlKey', 'shiftKey']) card.fire('click', { [key]: true });
  assert.deepEqual(history.state, { kept: true });
});

test('a list with no card to return to keeps the browser scroll restoration', async (t) => {
  const { history } = await open(t, { page: 'birds' }, { state: flock });
  assert.equal(history.scrollRestoration, 'auto');
});

test('a bird outside the list has no previous or next', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '1', q: 'heron' }, { state: flock });
  assert(linkNamed(main.children[0], 'Back to birds'));
  assert.equal(linkNamed(main, 'Previous'), undefined);
  assert.equal(linkNamed(main, 'Next'), undefined);
});

test('arrow keys open the previous and next bird outside text fields', async (t) => {
  const { doc, main, assigned } = await open(t, { page: 'bird', bird: '2' }, { state: flock });
  const key = (init) => doc.fire('keydown', { target: doc.body, ...init });
  key({ key: 'ArrowRight', target: new Element('input') });
  key({ key: 'ArrowRight', metaKey: true });
  assert.deepEqual(assigned, []);
  key({ key: 'ArrowRight' });
  key({ key: 'ArrowLeft' });
  assert.deepEqual(
    assigned.map((href) => params(href).bird),
    ['3', '1'],
  );
  assert(linkNamed(main, 'Next'));
});

test('slash moves to the bird search', async (t) => {
  const { doc, main } = await open(t, { page: 'birds' }, { state: flock });
  const event = doc.fire('keydown', { key: '/', target: doc.body });
  assert.equal(doc.activeElement, byId(main, 'bird-search'));
  assert(event.defaultPrevented);
});

test('the bird plate opens at full size', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '2' });
  const [image] = images(main).filter((node) => node.classList.contains('detail-art'));
  assert.equal(image.parent.tagName, 'A');
  assert.equal(image.parent.href, `/v1/owner/journal/birds/2/portrait?revision=${sha('b')}`);
});

test('field notes that fail to load leave the rest of the bird page working', async (t) => {
  const { main, problems } = await open(t, { page: 'bird', bird: '2' }, {
    recordProblems: true,
    profile: () => {
      throw Object.assign(new Error('Field notes are unavailable.'), { code: 'unavailable' });
    },
  });
  assert.deepEqual(problems, []);
  assert(findButton(main, 'Add to Favorites'), 'Favorites still works');
  assert(findButton(main, 'Add to Saved birds'), 'Saved birds still works');
  assert(nodes(main).some((node) => node.textContent === 'Field notes are unavailable.'));
});

test('field notes arrive below the bird actions, so nothing moves under a finger', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '2' }, {
    profile: () => ({ profile: null, notice: 'No field notes.' }),
  });
  const notes = main.querySelector('.bird-notes');
  const holding = (target) => notes.children.findIndex((child) => child === target || nodes(child).includes(target));
  const fieldNotes = notes.children.findIndex((node) => node.classList.contains('field-notes'));
  const favorites = holding(findButton(main, 'Add to Favorites'));
  const saved = holding(findButton(main, 'Add to Saved birds'));
  assert(favorites >= 0 && saved >= 0 && fieldNotes >= 0);
  assert(fieldNotes > favorites && fieldNotes > saved, 'field notes come after the actions');
});

test('an ended session while loading field notes still asks to sign in', async (t) => {
  const ended = Object.assign(new Error('Sign in again.'), { code: 'sign_in_required' });
  const { problems } = await open(t, { page: 'bird', bird: '2' }, {
    recordProblems: true,
    profile: () => {
      throw ended;
    },
  });
  assert.deepEqual(problems, [ended]);
});

test('the bird page shows its notes beside the plate', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '2' });
  await flush();
  const layout = main.querySelector('.bird-page');
  assert.deepEqual(
    layout.children.map((node) => node.className),
    ['bird-plate', 'bird-notes'],
  );
  assert(layout.children[0].contains(images(main).find((node) => node.classList.contains('detail-art'))));
  assert(layout.children[1].contains(findButton(main, 'Add to Favorites')));
});

const welcome = 'Welcome. Start wherever you like.';
const empty = { birds: [], active_taxon_ids: [], featured_taxon_id: null };
test('a new journal shows the welcome', async (t) => {
  const { main } = await open(t, { page: 'home' }, { state: withState(empty) });
  assert(main.textContent.includes(welcome));
});

for (const [name, change] of [
  ['a part that could not be read', { notices: [{ part: 'current-birds', message: 'Current birds could not be read.' }] }],
  ['unreadable history', { history_coverage: [{ source: 'ebird-archive', status: 'unavailable', coverage: 'Retained history needs attention.' }] }],
  ['unreadable plate work', { management: { available: false, reason: 'unreadable' } }],
])
  test('a journal with ' + name + ' and no birds is not shown as new', async (t) => {
    const { main } = await open(t, { page: 'home' }, { state: withState({ ...empty, ...change }) });
    assert(!main.textContent.includes(welcome));
    assert(main.textContent.includes('Make room for a bird'));
  });

for (const [plateReady, oneTap] of [
  [true, true],
  [false, false],
])
  test(`removing a saved bird ${plateReady ? 'with' : 'without'} approved art ${oneTap ? 'is one tap' : 'asks first'}`, async (t) => {
    const changes = [];
    const { main, assigned } = await open(t, { page: 'bird', bird: '4' }, {
      state: withState({
        birds: [{ ...bird(4, 'Demo Plover', 'd'), saved: true, plate_ready: plateReady }],
        saved_taxon_ids: [4],
      }),
      respond(path, body) {
        if (path === '/v1/owner/settings') return { revision: 'r'.repeat(64) };
        if (path === '/v1/owner/saved/preview')
          return { added_count: 0, removed_count: 1, migrated_legacy_queue_count: 0, collection_revision: sha('c') };
        if (path === '/v1/owner/saved/change') {
          changes.push(body);
          return { operation_id: 'op1', kind: 'saved-collection', status: 'succeeded', result: {} };
        }
        throw new Error('unexpected ' + path);
      },
    });
    findButton(main, 'Remove from Saved birds').click();
    await flush();
    assert.equal(changes.length, oneTap ? 1 : 0);
    assert.equal(Boolean(findButton(main, 'Confirm Saved birds change')), !oneTap);
    assert.equal(Boolean(findButton(main, 'Undo')), oneTap);
    assert.deepEqual(assigned, []);
  });
