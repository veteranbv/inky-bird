import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../src/inky_bird_frame/web/work-status.js', import.meta.url), 'utf8');
const { sourceState, sourceNote, nextCheck, requestState } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('saved success is not described as a currently active source', () => {
  assert.equal(sourceState({ enabled: true, status: 'ok' }, true), 'Checked');
  assert.doesNotMatch(sourceState({ enabled: true, status: 'ok' }, true), /now/);
  assert.match(sourceState({ enabled: true, activity: { status: 'running' } }, true), /Checking now/);
  assert.match(sourceState({ enabled: true, activity: { status: 'succeeded' } }, false), /out of date/);
  assert.match(sourceState({ enabled: true, activity: { status: 'unknown' } }, true), /Not heard from lately/);
  assert.equal(sourceState({ enabled: false, activity: { status: 'running' } }, true), 'Off');
});

test('scheduler times are checks, not promises, and overdue is explicitly unknown', () => {
  assert.match(nextCheck({}, false), /off/);
  assert.match(nextCheck({ status: 'running' }), /wait their turn/);
  assert.match(nextCheck({ next_run_at: '2026-11-01T00:00:00Z' }, true, Date.parse('2026-11-02T00:00:00Z')), /overdue/);
  assert.match(nextCheck({ next_run_at: '2026-11-03T00:00:00Z' }, true, Date.parse('2026-11-02T00:00:00Z')), /^Next check: about /);
});

test('preparation success is not confused with a later held or deferred plate', () => {
  const request = { taxon_id: 42, status: 'eligible' };
  assert.match(requestState(request, { needs_attention: [42] }), /waiting for you to ask again/);
  assert.match(requestState(request, { waiting: [42] }), /waiting a while before it tries again/);
  assert.match(requestState({ status: 'blocked', outcome: 'interrupted' }, {}), /not started again/);
  assert.equal(requestState({ status: 'approved' }, {}), 'Already has a plate');
});

// A tab left open across an upgrade can meet a value this page does not know.
test('an unknown request status is not reported as finished', () => {
  const text = requestState({ taxon_id: 7, status: 'future-status' }, {});
  assert.doesNotMatch(text, /Ready|future-status/);
  assert.match(text, /could not read/);
  assert.equal(requestState({ taxon_id: 7, status: 'eligible' }, {}), 'Ready to be tried again');
});

test('an incomplete saved check is named', () => {
  assert.match(sourceState({ enabled: true, status: 'degraded' }, true), /incomplete/);
  assert.equal(sourceState({ enabled: true, status: 'not_checked' }, true), 'Not checked yet');
});

test('a partial check and birds kept from the last good check are said plainly', () => {
  assert.equal(sourceNote({ enabled: true, status: 'ok', partial: true }), 'Its last check read only part of its history.');
  assert.equal(sourceNote({ enabled: true, status: 'error', carried_species_count: 3 }), 'Showing its last birds for now.');
  assert.equal(sourceNote({ enabled: true, status: 'error', carried_species_count: 0 }), '');
  assert.equal(sourceNote({ enabled: true, status: 'ok', partial: false }), '');
  assert.equal(sourceNote({ enabled: true, status: 'ok' }), '');
  assert.equal(sourceNote({ enabled: false, status: 'error', carried_species_count: 3, partial: true }), '');
});
