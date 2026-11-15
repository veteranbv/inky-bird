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
  assert.equal(
    words.whyThisBird(bird([], [row('birdbuddy', 1, '2026-01-01', '2026-11-12')]), { today }),
    'Noted at your feeder this week',
  );
  assert.equal(words.whyThisBird(bird(['inaturalist'], []), { window: 'last-week', today }), 'Seen nearby this week');
  assert.equal(words.whyThisBird(bird(['ebird'], []), { window: 'last-30-days', today }), 'Seen nearby this month');
  assert.equal(words.whyThisBird(bird(['ebird'], []), { window: 'all-time', today }), 'Seen nearby');
  assert.equal(words.whyThisBird(bird(['birdweather'], []), { window: 'last-day', today }), 'Noted on your microphone this week');
  assert.equal(
    words.whyThisBird(bird([], [row('ebird-archive', 3, '2019-03-12', '2020-05-01')]), { today }),
    'From your journal, 2019',
  );
  // A record older than a week, or dated in the future, is not "this week".
  assert.equal(
    words.whyThisBird(bird([], [row('birdbuddy', 1, null, '2026-11-01'), row('birdbuddy', 1, null, '2026-12-01')]), { today }),
    '',
  );
  assert.equal(words.whyThisBird(bird(['owner-selection'], []), { today }), '');
  assert.equal(words.whyThisBird(null), '');
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
