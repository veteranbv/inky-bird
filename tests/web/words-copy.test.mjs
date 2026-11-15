import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../src/inky_bird_frame/web/words.js', import.meta.url), 'utf8');
const words = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test('observation windows read as plain words, never as their keys', () => {
  assert.equal(words.windowName('last-30-days'), 'Last 30 days');
  assert.equal(words.windowName('all-time'), 'All time');
  assert.equal(words.windowName('last-fortnight'), 'Last fortnight');
  assert.equal(words.windowName(undefined), '');
});

test('notification events read as plain words', () => {
  assert.equal(words.eventName('generation_approved'), 'New plates');
  assert.equal(words.eventName('display_stale'), 'The wall frame stopped updating');
  assert.equal(words.eventName('some_new_event'), 'Some new event');
});

test('a moment reads as today, yesterday, a weekday or a date', () => {
  const now = new Date(2026, 10, 15, 18, 0);
  assert.equal(words.when(new Date(2026, 10, 15, 9, 5).toISOString(), now), 'today at 09:05');
  assert.equal(words.when(new Date(2026, 10, 14, 21, 30).toISOString(), now), 'yesterday at 21:30');
  assert.equal(words.when(new Date(2026, 10, 10, 12, 0).toISOString(), now), 'on Tuesday');
  assert.equal(words.when(new Date(2019, 2, 12, 12, 0).toISOString(), now), '12 Mar 2019');
  assert.equal(words.when(null, now), '');
  assert.equal(words.when('not a date', now), '');
});
