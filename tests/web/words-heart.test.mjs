import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../src/inky_bird_frame/web/words.js', import.meta.url), 'utf8');
const words = await import(`data:text/javascript,${encodeURIComponent(source)}`);

const today = new Date(2026, 10, 14);
const row = (source, count, first_noted, last_noted) => ({ source, count, unit: 'u', first_noted, last_noted });

test('the journal line gives first and last noted dates and the record count', () => {
  assert.equal(
    words.journalLine([row('ebird-archive', 9, '2019-03-12', '2026-10-01'), row('birdbuddy', 5, '2020-01-02', '2026-11-02')]),
    'In your journal: first noted 12 Mar 2019 · last noted 2 Nov 2026 · 14 records',
  );
});

test('one source is named in the owner terms, one record is singular', () => {
  assert.equal(
    words.journalLine([row('birdbuddy', 1, '2026-11-02', '2026-11-02')]),
    'In your journal: noted 2 Nov 2026 · 1 record at your feeder',
  );
  assert.equal(
    words.journalLine([row('ebird-archive', 3, '2019-03-12', '2020-05-01')]),
    'In your journal: first noted 12 Mar 2019 · last noted 1 May 2020 · 3 records from your checklists',
  );
  assert.equal(
    words.journalLine([row('birdnet-analyzer', 2, null, '2020-05-01')]),
    'In your journal: last noted 1 May 2020 · 2 records on your microphone',
  );
});

test('missing parts are left out and nothing recorded says nothing', () => {
  assert.equal(words.journalLine([row('birdnet-analyzer', 4, null, null)]), 'In your journal: 4 records on your microphone');
  assert.equal(words.journalLine([row('ebird-archive', 0, null, null)]), '');
  assert.equal(words.journalLine([]), '');
  assert.equal(words.journalLine(undefined), '');
});

test('the noted clause names the latest dated record and when', () => {
  assert.equal(words.notedClause([row('birdbuddy', 2, '2026-01-01', '2026-11-10')], today), 'noted at your feeder on Tuesday');
  assert.equal(words.notedClause([row('ebird-archive', 2, '2019-01-01', '2019-03-12')], today), 'noted in your checklists on 12 Mar 2019');
  assert.equal(words.notedClause([row('birdbuddy', 2, null, '2026-11-14')], today), 'noted at your feeder today');
  assert.equal(words.notedClause([row('birdbuddy', 2, null, null)], today), '');
  assert.equal(words.notedClause([], today), '');
});

test('why this bird comes only from data that supports it', () => {
  const bird = (sources, evidence) => ({ sources, retained_evidence: evidence });
  const seen = (sources, latest_detection_at, timestamp_source = sources.length === 1 ? sources[0] : null) => ({
    sources,
    latest_detection_at,
    timestamp_source,
  });
  assert.equal(
    words.whyThisBird(bird(['birdbuddy'], [row('birdbuddy', 1, '2026-01-01', '2026-11-12')]), { today }),
    'Noted at your feeder this week',
  );
  const nearby = bird(['inaturalist'], []);
  assert.equal(words.whyThisBird(nearby, { detection: seen(['inaturalist'], '2026-11-12T08:00:00Z'), today }), 'Seen nearby this week');
  assert.equal(
    words.whyThisBird(bird(['birdweather'], []), { detection: seen(['birdweather'], '2026-11-14T05:00:00Z'), today }),
    'Noted on your microphone this week',
  );
  // Two live sources with different places: the line names none.
  assert.equal(
    words.whyThisBird(bird(['birdweather', 'ebird'], []), { detection: seen(['birdweather', 'ebird'], '2026-11-13T05:00:00Z'), today }),
    'Noted this week',
  );
  assert.equal(
    words.whyThisBird(bird(['ebird-archive'], [row('ebird-archive', 3, '2019-03-12', '2020-05-01')]), { today }),
    'From your journal, 2019',
  );
  // A live source alone, without a recent dated detection, says nothing.
  assert.equal(words.whyThisBird(nearby, { today }), '');
  assert.equal(words.whyThisBird(nearby, { detection: seen(['inaturalist'], '2026-10-01T08:00:00Z'), today }), '');
  assert.equal(words.whyThisBird(nearby, { detection: seen(['inaturalist'], '2026-11-20T08:00:00Z'), today }), '');
  assert.equal(words.whyThisBird(nearby, { detection: seen(['inaturalist'], 'nonsense'), today }), '');
  // A record older than a week, or dated in the future, is not "this week".
  assert.equal(
    words.whyThisBird(bird([], [row('birdbuddy', 1, null, '2026-11-01'), row('birdbuddy', 1, null, '2026-12-01')]), { today }),
    '',
  );
  assert.equal(words.whyThisBird(bird(['owner-selection'], []), { today }), '');
  assert.equal(words.whyThisBird(null), '');
});

test('a bird known only from an old postcard is not shown as noted lately', () => {
  // The journal lists a retained record's source among the bird's sources.
  const postcard = {
    sources: ['birdbuddy'],
    retained_evidence: [{ source: 'birdbuddy', count: 2, unit: 'postcards', first_noted: '2019-03-12', last_noted: '2019-04-01' }],
  };
  assert.equal(words.whyThisBird(postcard, { today }), 'From your journal, 2019');
  assert.equal(words.whyThisBird({ ...postcard, retained_evidence: [{ ...postcard.retained_evidence[0], first_noted: null }] }, { today }), '');
});

test('counts in different units are not added into one total', () => {
  assert.equal(
    words.journalLine([
      { source: 'ebird-archive', count: 3, unit: 'checklists', first_noted: '2019-03-12', last_noted: '2020-05-01' },
      { source: 'birdnet-analyzer', count: 500, unit: 'detections', first_noted: '2021-01-01', last_noted: '2026-11-02' },
    ]),
    'In your journal: first noted 12 Mar 2019 · last noted 2 Nov 2026',
  );
});

test('a record dated in the future is not named as noted', () => {
  assert.equal(words.notedClause([row('birdbuddy', 2, null, '2026-11-20')], today), '');
  assert.equal(
    words.notedClause([row('birdbuddy', 2, null, '2026-11-20'), row('ebird-archive', 1, null, '2026-11-10')], today),
    'noted in your checklists on Tuesday',
  );
});

test('short dates read as a day name or a plain date', () => {
  assert.equal(words.shortDate('2026-11-14', today), 'Today');
  assert.equal(words.shortDate('2026-11-13', today), 'Yesterday');
  assert.equal(words.shortDate('2026-11-10', today), 'Tuesday');
  assert.equal(words.shortDate('2019-03-12', today), '12 Mar 2019');
  assert.equal(words.shortDate('nonsense', today), '');
});

test('plate counts separate new plates from plates that need a hand', () => {
  const management = {
    available: true,
    work: {
      needs_attention: [5],
      in_review: { candidates: [{ inspect_available: true }, { inspect_available: true }, { inspect_available: false }] },
    },
  };
  assert.deepEqual(words.plateCounts(management), { newPlates: 2, needsAHand: 2 });
  assert.deepEqual(words.plateCounts({ available: false }), { newPlates: 0, needsAHand: 0 });
  assert.deepEqual(words.plateCounts(undefined), { newPlates: 0, needsAHand: 0 });
});

test('a bird with records in different units and no dates is still in the journal', () => {
  assert.equal(
    words.journalLine([
      { source: 'birdbuddy', count: 2, unit: 'postcards', first_noted: null, last_noted: null },
      { source: 'ebird-archive', count: 3, unit: 'checklists', first_noted: null, last_noted: null },
    ]),
    'In your journal: records without dates',
  );
});
