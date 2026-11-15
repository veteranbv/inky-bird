import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../src/inky_bird_frame/web/work-status.js', import.meta.url), 'utf8');
const { sourceState, nextCheck, requestState } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('saved success is not described as a currently active source', () => {
  assert.match(sourceState({ enabled: true, status: 'ok' }, true), /saved check/);
  assert.match(sourceState({ enabled: true, activity: { status: 'running' } }, true), /Checking now/);
  assert.match(sourceState({ enabled: true, activity: { status: 'succeeded' } }, false), /stale/);
  assert.match(sourceState({ enabled: true, activity: { status: 'unknown' } }, true), /unknown/);
  assert.equal(sourceState({ enabled: false, activity: { status: 'running' } }, true), 'Off');
});

test('scheduler times are checks, not promises, and overdue is explicitly unknown', () => {
  assert.match(nextCheck({}, false), /off/);
  assert.match(nextCheck({ status: 'running' }), /wait behind/);
  assert.match(nextCheck({ next_run_at: '2026-11-01T00:00:00Z' }, true, Date.parse('2026-11-02T00:00:00Z')), /not confirmed/);
  assert.match(nextCheck({ next_run_at: '2026-11-03T00:00:00Z' }, true, Date.parse('2026-11-02T00:00:00Z')), /not a promised/);
});

test('preparation success is not confused with a later held or deferred plate', () => {
  const request = { taxon_id: 42, status: 'eligible' };
  assert.match(requestState(request, { needs_attention: [42] }), /later attempt is held/);
  assert.match(requestState(request, { waiting: [42] }), /backoff/);
  assert.match(requestState({ status: 'blocked', outcome: 'interrupted' }, {}), /not replayed/);
  assert.match(requestState({ status: 'approved' }, {}), /no regeneration/);
});

// A tab left open across an upgrade can meet a value this page does not know.
test('an unknown request status is not reported as finished', () => {
  const text = requestState({ taxon_id: 7, status: 'future-status' }, {});
  assert.doesNotMatch(text, /completed|eligible/);
  assert.match(text, /future-status/);
  assert.match(requestState({ taxon_id: 7, status: 'eligible' }, {}), /eligible under the current policy/);
});

test('an incomplete saved check is named', () => {
  assert.match(sourceState({ enabled: true, status: 'degraded' }, true), /incomplete/);
  assert.equal(sourceState({ enabled: true, status: 'not_checked' }, true), 'No recorded check');
});
