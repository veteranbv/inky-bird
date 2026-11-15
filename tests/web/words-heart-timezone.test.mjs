// Times are read in the owner's own time zone, not by their UTC date.
process.env.TZ = 'America/New_York';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../src/inky_bird_frame/web/words.js', import.meta.url), 'utf8');
const words = await import(`data:text/javascript,${encodeURIComponent(source)}`);

const today = new Date(2026, 10, 15, 21, 0);
const nearby = { sources: ['inaturalist'], retained_evidence: [] };
const seen = (latest_detection_at) => ({ sources: ['inaturalist'], latest_detection_at, timestamp_source: 'inaturalist' });

test('the time zone under test is not UTC', () => {
  assert.equal(today.getTimezoneOffset(), 300);
});

test('a detection this evening is this week even when its UTC date is tomorrow', () => {
  assert.equal(words.whyThisBird(nearby, { detection: seen('2026-11-16T01:30:00+00:00'), today }), 'Seen nearby this week');
});

test('a time written with a space instead of T is read the same way', () => {
  assert.equal(words.whyThisBird(nearby, { detection: seen('2026-11-16 01:30:00+00:00'), today }), 'Seen nearby this week');
});

test('a detection seven local days ago is not this week', () => {
  assert.equal(words.whyThisBird(nearby, { detection: seen('2026-11-09T02:00:00Z'), today }), '');
});

test('date-only records keep their calendar day', () => {
  const row = { source: 'birdbuddy', count: 1, unit: 'u', first_noted: '2026-11-09', last_noted: '2026-11-09' };
  assert.equal(words.whyThisBird({ sources: ['birdbuddy'], retained_evidence: [row] }, { today }), 'Noted at your feeder this week');
});
