import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../src/inky_bird_frame/web/words.js', import.meta.url), 'utf8');
const words = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test('counts use the singular for one and the plural otherwise', () => {
  assert.equal(words.plural(1, 'record', 'records'), '1 record');
  assert.equal(words.plural(0, 'record', 'records'), '0 records');
  assert.equal(words.plural(14, 'record', 'records'), '14 records');
});

test('dates read as a day, short month and year', () => {
  assert.equal(words.plainDate('2019-03-12'), '12 Mar 2019');
  assert.equal(words.plainDate('2026-11-02T08:00:00Z'), '2 Nov 2026');
  assert.equal(words.plainDate('2026-02-30'), '');
  assert.equal(words.plainDate(null), '');
});

test('recent dates read as today, yesterday or a weekday', () => {
  const today = new Date(2026, 10, 14);
  assert.equal(words.recentDate('2026-11-14', today), 'today');
  assert.equal(words.recentDate('2026-11-13', today), 'yesterday');
  assert.equal(words.recentDate('2026-11-10', today), 'on Tuesday');
  assert.equal(words.recentDate('2026-11-07', today), '7 Nov 2026');
  assert.equal(words.recentDate('2026-11-20', today), '20 Nov 2026');
  assert.equal(words.recentDate('nonsense', today), '');
});

test('each thing has one name', () => {
  const names = Object.values(words.WORDS);
  assert.equal(new Set(names).size, names.length);
  assert.equal(words.WORDS.onMyFrame, 'On my frame');
  assert.equal(words.WORDS.play, 'Play');
  assert.equal(words.WORDS.wallFrame, 'Wall frame');
});
