// Home, bird and new-plate pages: what the owner reads.
import assert from 'node:assert/strict';
import test from 'node:test';
import { findButton } from './fake-dom.mjs';
import { bird, flush, images, journal, nodes, open, sha, withState } from './journal-harness.mjs';

const texts = (root) => nodes(root).filter((node) => !node.children.length).map((node) => node.textContent);
const has = (root, text) => texts(root).includes(text);
const linkTo = (root, text) => nodes(root).find((node) => node.tagName === 'A' && node.textContent === text);
const params = (href) => Object.fromEntries(new URL(href, 'https://inky.example.test').searchParams);
const day = (offset) => {
  const now = new Date();
  const value = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset);
  return [value.getFullYear(), value.getMonth() + 1, value.getDate()].map((n) => String(n).padStart(2, '0')).join('-');
};
const evidence = (source, count, first_noted, last_noted) => ({ source, count, unit: 'u', first_noted, last_noted });
const work = (candidates, needs_attention = []) => ({
  available: true,
  sources: [{ name: 'ebird-archive', enabled: true, status: 'ok' }],
  work: { complete: true, needs_attention, in_review: { candidates } },
  notifications: { dead_letters: 0 },
});

test('the bird page says what the journal holds for this bird', async (t) => {
  const state = withState({
    birds: [
      {
        ...bird(2, 'Sample Finch', 'b'),
        retained_evidence: [evidence('ebird-archive', 9, '2019-03-12', '2026-10-01'), evidence('birdbuddy', 5, '2020-01-02', '2026-11-02')],
      },
    ],
  });
  const { main } = await open(t, { page: 'bird', bird: '2' }, { state });
  assert(has(main, 'In your journal: first noted 12 Mar 2019 · last noted 2 Nov 2026 · 14 records'));
  assert(!main.textContent.includes('retained history'));
  assert(!main.textContent.includes('0 records'));
});

test('a bird with nothing recorded has no journal line', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '2' });
  assert(!main.textContent.includes('In your journal:'));
});

test('Home shows the three latest birds with their dates and the journal size', async (t) => {
  const state = withState({
    birds: [
      bird(1, 'Example Wren', 'a'),
      bird(2, 'Sample Finch', 'b'),
      bird(3, 'Test Heron', 'c'),
      { ...bird(4, 'Demo Plover', 'd'), plate_ready: false, portrait_url: null },
      { ...bird(5, 'Unknown Gull', 'e'), known: false },
    ],
    recent_species: [
      { taxon_id: 3, last_noted: day(0), source: 'birdbuddy' },
      { taxon_id: 99, last_noted: day(1), source: 'ebird-archive' },
      { taxon_id: 2, last_noted: day(1), source: 'ebird-archive' },
      { taxon_id: 4, last_noted: '2019-03-12', source: 'ebird-archive' },
      { taxon_id: 1, last_noted: '2018-01-01', source: 'ebird-archive' },
    ],
  });
  const { main } = await open(t, { page: 'home' }, { state });
  const lately = main.querySelector('.lately');
  assert(lately, 'the Lately strip is shown');
  assert(has(lately, 'Lately'));
  const entries = nodes(lately).filter((node) => node.tagName === 'A');
  assert.deepEqual(
    entries.map((node) => [params(node.href).bird, node.textContent]),
    [
      ['3', 'Test Heron Today'],
      ['2', 'Sample Finch Yesterday'],
      ['4', 'Demo Plover 12 Mar 2019'],
    ],
  );
  assert(has(main, '4 birds in your journal · 3 with plates'));
});

test('Home without history shows no Lately strip', async (t) => {
  const { main } = await open(t, { page: 'home' }, { state: withState({ recent_species: [] }) });
  assert.equal(main.querySelector('.lately'), null);
  assert(!main.textContent.includes('Lately'));
  assert(has(main, '3 birds in your journal · 3 with plates'));
});

test('one plate in the journal is counted in the singular', async (t) => {
  const { main } = await open(t, { page: 'home' }, {
    state: withState({ birds: [bird(1, 'Example Wren', 'a')] }),
  });
  assert(has(main, '1 bird in your journal · 1 with a plate'));
});

test('the featured plate says why it is shown when the data supports it', async (t) => {
  const state = withState({
    birds: [{ ...bird(1, 'Example Wren', 'a'), retained_evidence: [evidence('birdbuddy', 2, day(30), day(2))] }],
  });
  const { main } = await open(t, { page: 'home' }, { state });
  const line = nodes(main.querySelector('.featured-caption')).find((node) => node.classList.contains('why-this-bird'));
  assert.equal(line?.textContent, 'Noted at your feeder this week');
});

test('a featured bird known only from an old postcard is not shown as noted lately', async (t) => {
  // The journal lists a retained record's source among the bird's sources.
  const state = withState({
    birds: [
      {
        ...bird(1, 'Example Wren', 'a'),
        sources: ['birdbuddy'],
        retained_evidence: [{ source: 'birdbuddy', count: 2, unit: 'postcards', first_noted: '2019-03-12', last_noted: '2019-04-01' }],
      },
    ],
    management: { ...work([]), window: 'last-week', discovery_refreshed_at: '2026-01-01T00:00:00+00:00', recent_detections: [] },
  });
  const { main } = await open(t, { page: 'home' }, { state });
  assert.equal(main.querySelector('.why-this-bird')?.textContent, 'From your journal, 2019');
});

const recent = (offset) => day(offset) + 'T12:00:00+00:00';
const detection = (taxon_id, latest_detection_at) => ({
  taxon_id,
  sources: ['inaturalist'],
  timestamp_source: 'inaturalist',
  latest_detection_at,
});
for (const [name, detections, expected] of [
  ['seen nearby this week says so', [detection(1, recent(1))], 'Seen nearby this week'],
  ['last seen weeks ago has no reason line', [detection(1, recent(20))], null],
  ['without its own detection has no reason line', [detection(2, recent(1))], null],
])
  test('a featured bird ' + name, async (t) => {
    const state = withState({
      birds: [{ ...bird(1, 'Example Wren', 'a'), sources: ['inaturalist'] }],
      management: { ...work([]), window: 'last-week', recent_detections: detections },
    });
    const { main } = await open(t, { page: 'home' }, { state });
    assert.equal(main.querySelector('.why-this-bird')?.textContent ?? null, expected);
  });

test('a featured plate without supporting data has no reason line', async (t) => {
  const { main } = await open(t, { page: 'home' });
  assert.equal(main.querySelector('.why-this-bird'), null);
});

test('new plates are good news on Home and are not repeated elsewhere', async (t) => {
  const candidates = [
    { taxon_id: 8, inspect_available: true },
    { taxon_id: 9, inspect_available: true },
    { taxon_id: 10, inspect_available: false },
  ];
  const state = withState({ management: work(candidates, [11]) });
  const { main } = await open(t, { page: 'home' }, { state });
  const card = linkTo(main, '2 new plates are ready to look at.');
  assert(card, 'the new plates card links to them');
  assert.equal(params(card.href).page, 'work');
  assert.equal(main.querySelector('.attention-banner'), null);
  assert(!main.textContent.includes('held for recovery'));
  assert(!main.textContent.includes('stopped'));
  assert(!main.textContent.includes('needs a hand'));
});

test('other pages carry no new-plates banner; the Manage tab counts them', async (t) => {
  const candidates = [
    { taxon_id: 8, inspect_available: true },
    { taxon_id: 9, inspect_available: true },
    { taxon_id: 10, inspect_available: false },
  ];
  const { doc, main } = await open(t, { page: 'birds' }, { state: withState({ management: work(candidates, [11]) }) });
  assert(!main.textContent.includes('new plate'));
  assert(!main.textContent.includes('held for recovery'));
  assert.equal(main.querySelector('.attention-banner'), null);
  const nav = nodes(doc.body).find((node) => node.id === 'journal-navigation');
  const manage = nodes(nav).find((node) => node.tagName === 'A' && params(node.href).page === 'manage');
  assert.equal(manage.getAttribute('aria-label'), 'Manage, 2 new plates');
  assert.equal(manage.querySelector('.tab-count').textContent, '2');
});

test('the Manage tab has no count when no new plate is waiting', async (t) => {
  const { doc } = await open(t, { page: 'birds' }, { state: withState({ management: work([{ taxon_id: 10, inspect_available: false }]) }) });
  const nav = nodes(doc.body).find((node) => node.id === 'journal-navigation');
  const manage = nodes(nav).find((node) => node.tagName === 'A' && params(node.href).page === 'manage');
  assert.equal(manage.getAttribute('aria-label'), null);
  assert.equal(manage.querySelector('.tab-count'), null);
});

test('one new plate opens directly', async (t) => {
  const state = withState({ management: work([{ taxon_id: 8, inspect_available: true }]) });
  const { main } = await open(t, { page: 'home' }, { state });
  const card = linkTo(main, '1 new plate is ready to look at.');
  assert.deepEqual(params(card.href), { page: 'candidate', bird: '8' });
});

test('a source that needs the owner is still mentioned on Home, plainly', async (t) => {
  const management = work([]);
  management.sources = [{ name: 'ebird-archive', enabled: true, status: 'error' }];
  management.notifications = { dead_letters: 2 };
  const { main } = await open(t, { page: 'home' }, { state: withState({ management }) });
  assert.equal(params(linkTo(main, 'A source check failed.').href).page, 'sources');
  assert.equal(params(linkTo(main, 'Alerts are not being delivered.').href).page, 'notifications');
});

const nothingOnFrame = { active_taxon_ids: [], featured_taxon_id: null };
test('an empty frame features a plate from the catalog and invites a start', async (t) => {
  const { main } = await open(t, { page: 'home' }, { state: withState(nothingOnFrame) });
  const hero = main.querySelector('.featured-art');
  assert(hero, 'a plate is featured');
  assert.equal(images(hero).length, 1);
  assert(has(main, 'Pick a few birds you love to start your frame.'));
  assert(linkTo(main, 'Choose birds'));
  assert(!main.textContent.includes('Make room for a bird'));
  assert(!main.textContent.includes('Welcome. Start'));
});

test('a new install also offers a source, as optional', async (t) => {
  const fresh = withState({ ...nothingOnFrame, birds: journal().birds.map((item) => ({ ...item, known: false })) });
  const { main } = await open(t, { page: 'home' }, { state: fresh });
  assert(has(main, 'Pick a few birds you love to start your frame.'));
  assert.equal(params(linkTo(main, 'Connect a source (optional)').href).page, 'sources');
});

test('an empty frame with an unreadable part is not offered a first start', async (t) => {
  const state = withState({
    ...nothingOnFrame,
    birds: journal().birds.map((item) => ({ ...item, known: false })),
    notices: [{ part: 'current-birds', message: 'The current wall birds could not be read.' }],
  });
  const { main } = await open(t, { page: 'home' }, { state });
  assert(has(main, 'The current wall birds could not be read.'));
  assert.equal(linkTo(main, 'Connect a source (optional)'), undefined);
  // The frame may already hold birds, so Home does not say it is empty.
  assert(!main.textContent.includes('to start your frame'));
  assert.equal(main.querySelector('.featured-art'), null, 'no stand-in plate for a frame that may not be empty');
});

test('bird cards and the list use the shared names', async (t) => {
  const state = withState({
    birds: [{ ...bird(1, 'Example Wren', 'a'), known: false }, { ...bird(2, 'Sample Finch', 'b'), saved: true }],
  });
  const art = await open(t, { page: 'birds', scope: 'art' }, { state });
  assert.equal(art.main.querySelector('h1').textContent, 'More plates');
  assert(has(art.main, 'Plate available'));
  assert(has(art.main, 'On my frame'));
  for (const old of ['Available artwork', 'not a sighting', 'Saved selection', 'Current and saved'])
    assert(!art.main.textContent.includes(old), old);
  const show = nodes(art.main).find((node) => node.id === 'bird-show');
  assert(nodes(show).some((node) => node.textContent === 'On my frame'));
  const nav = nodes(art.doc.body).find((node) => node.id === 'journal-navigation');
  assert(nodes(nav).some((node) => node.textContent === 'Play'));
  assert(!nodes(nav).some((node) => node.textContent === 'Frame'));
});

test('the bird page has no stacked Favorites disclaimer', async (t) => {
  const { main } = await open(t, { page: 'bird', bird: '2' });
  assert(!main.textContent.includes('bookmarks'));
  assert(findButton(main, 'Add to Favorites'));
});

function candidate(change = {}) {
  return {
    taxon_id: 7,
    common_name: 'Sample Finch',
    scientific_name: 'Exemplum sample finch',
    notice: 'Server notice.',
    portrait_url: '/candidate.png',
    candidate_revision: sha('c'),
    can_approve: true,
    profile: null,
    quality_review: {
      passed: true,
      species_accuracy: 5,
      anatomy_accuracy: 4,
      text_accuracy: 5,
      composition_quality: 5,
      location_free: true,
    },
    verification_sources: [],
    ...change,
  };
}
const candidateResponder = (value) => (path) => {
  if (path === '/v1/owner/candidates/7') return value;
  throw new Error('unexpected ' + path);
};

test('a new plate arrives as a new page for the journal', async (t) => {
  const state = withState({
    birds: [{ ...bird(7, 'Sample Finch', 'b'), retained_evidence: [evidence('birdbuddy', 1, '2019-01-01', '2019-03-12')] }],
  });
  const { main } = await open(t, { page: 'candidate', bird: '7' }, { state, respond: candidateResponder(candidate()) });
  assert.equal(main.querySelector('h1').textContent, 'A new page for your journal');
  assert(has(main, 'Sample Finch — noted at your feeder on 12 Mar 2019. Keep it, or send it back.'));
  for (const sentence of ['Checks passed', 'No place details in the image'])
    assert(has(main, sentence), sentence);
  for (const raw of ['passed', 'location free', 'true', 'species accuracy', 'Generated art'])
    assert(!texts(main).includes(raw) && !main.textContent.includes(raw + ' '), raw);
  assert(!main.textContent.includes('Server notice.'), 'a ready plate needs no server caveat');
  assert(findButton(main, 'Confirm local approval'));
  assert(findButton(main, 'Confirm rejection'));
  assert(has(main, 'This note is sent to the image service to guide the next attempt.'));
  const plate = images(main)[0];
  const heading = main.querySelector('h2');
  assert(main.children.indexOf(plate.parent) <= main.children.indexOf(heading.parent), 'the plate leads');
});

test('a new plate without a record of the bird leaves the noted clause out', async (t) => {
  const { main } = await open(t, { page: 'candidate', bird: '7' }, { respond: candidateResponder(candidate()) });
  assert(has(main, 'Sample Finch. Keep it, or send it back.'));
});

test('a plate that did not pass says so and keeps the server reason', async (t) => {
  const value = candidate({
    can_approve: false,
    notice: 'This candidate does not meet ordinary publication checks.',
    quality_review: { passed: false, location_free: false },
  });
  const { main } = await open(t, { page: 'candidate', bird: '7' }, { respond: candidateResponder(value) });
  // A plate that cannot be kept is not offered as one.
  assert(has(main, 'Sample Finch.'));
  assert(has(main, 'Checks did not pass'));
  assert(has(main, 'The image may show place details'));
  assert(has(main, 'This candidate does not meet ordinary publication checks.'));
  assert.equal(findButton(main, 'Confirm local approval'), null);
  assert(findButton(main, 'Confirm rejection'));
  await flush();
});
