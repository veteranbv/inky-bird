import assert from 'node:assert/strict';
import test from 'node:test';
import { nodes, open, withState } from './journal-harness.mjs';

const shownText = (main) =>
  nodes(main)
    .filter((node) => !node.hidden && !node.children.length)
    .map((node) => node.textContent);

test('the Sources page says when a check was partial or old birds are still shown', async (t) => {
  const { main } = await open(t, { page: 'sources' }, {
    state: withState({
      management: {
        available: true,
        window: 'last-30-days',
        refresh_minutes: 15,
        sources: [
          { name: 'birdbuddy', enabled: true, status: 'ok', partial: true, species_count: 2, activity: {} },
          { name: 'birdweather', enabled: true, status: 'error', carried_species_count: 3, species_count: 0, activity: {} },
          { name: 'inaturalist', enabled: true, status: 'ok', partial: false, species_count: 1, activity: {} },
        ],
        work: { complete: true, discovery_status: 'ok' },
        activity: { entries: {} },
        recent_detections: [],
      },
    }),
    respond(path) {
      if (path === '/v1/owner/settings') return { revision: null };
      if (path === '/v1/owner/operations') return { operations: [] };
      throw new Error('unexpected ' + path);
    },
  });
  const shown = shownText(main);
  const partial = 'Its last check read only part of its history.';
  const carried = 'Showing its last birds for now.';
  assert.equal(shown.filter((text) => text === partial).length, 1);
  assert.equal(shown.filter((text) => text === carried).length, 1);
  const rows = nodes(main).filter((node) => node.classList?.contains('source-row'));
  const rowText = (name) => {
    const row = rows.find((node) => nodes(node).some((child) => child.tagName === 'H2' && child.textContent === name));
    assert(row, name);
    return nodes(row).filter((node) => !node.hidden).map((node) => node.textContent);
  };
  assert(rowText('Bird Buddy').includes(partial));
  assert(rowText('BirdWeather').includes(carried));
  const plain = rowText('iNaturalist');
  assert(!plain.includes(partial) && !plain.includes(carried));
});
