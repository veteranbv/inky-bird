// Saved birds, collections, history imports and operation outcomes.
// Run through journal.e2e.cjs only: phases run in its order and hand state
// to later phases through t. historyAndCollections leaves t.trip and
// t.imported, which frame and manage phases read.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Owner operations finish on a timer the page polls; the test clock drives it.
async function shown(page, text) {
  for (let n = 0; n < 20; n++) {
    if (await page.getByText(text, { exact: true }).isVisible()) return;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText(text, { exact: true }).waitFor();
}

// Favorites, eBird history, trips, portable collections and missing-plate requests.
async function historyAndCollections(t) {
  const { page, context, origin, fixture, heading, step, state, cardinal, robin } = t;
  t.phase = 'favorites';
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
  await heading('Northern Cardinal');
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Add to Favorites', exact: true }).click(),
  ]);
  await heading('Northern Cardinal');
  assert.equal(state().collections[0].kind, 'favorites');
  t.phase = 'upload';
  await page.goto(origin + '/app?page=imports&kind=ebird-archive');
  await heading('Your eBird history');
  await page
    .getByLabel('History file', { exact: true })
    .setInputFiles(path.join(fixture, 'archive.csv'));
  // The next step replaces the upload status at once, so record each text it shows.
  await page.locator('form [role="status"]').evaluate((status) => {
    window.uploadStatuses = [];
    new MutationObserver(() => window.uploadStatuses.push(status.textContent)).observe(status, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  });
  await page.getByRole('button', { name: 'Upload for checking', exact: true }).click();
  await step('2. Check the file');
  assert(
    (await page.evaluate(() => window.uploadStatuses)).some((text) =>
      /^Uploading privately: 100% \(\d+ of \d+ KiB\)/.test(text),
    ),
  );
  assert(!fs.existsSync(path.join(fixture, 'var/controller/ebird-archive-observations.json')));
  t.phase = 'preview';
  await page.getByRole('button', { name: 'Check this file', exact: true }).click();
  await step('3. Review history changes');
  await page.reload();
  await heading('Your eBird history');
  await heading('3. Review history changes');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/ebird-archive-observations.json')));
  t.phase = 'history-confirmation';
  await page.getByRole('button', { name: 'Confirm history update', exact: true }).click();
  await step('4. Choose your checklists');
  assert.equal(state().collections.length, 1);
  await page.getByLabel('Trip start date', { exact: true }).fill('2001-03-01');
  await page.getByLabel('Trip end date', { exact: true }).fill('2001-03-02');
  await page.getByLabel('Trip name', { exact: true }).fill('A synthetic spring trip');
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('checkbox', { name: /2001-03-01/ }).check();
  assert.equal(await page.getByRole('checkbox').count(), 2);
  t.phase = 'trip-preview';
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  await page.reload();
  await heading('Your eBird history');
  await heading('5. Save your trip');
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: fixture + '/trip-review-phone.png', fullPage: true });
  t.phase = 'stale-trip-and-recovery';
  const session = (await (await context.request.get(origin + '/v1/owner/session')).json()).data;
  const journal = (await (await context.request.get(origin + '/v1/owner/journal')).json()).data;
  const edited = await context.request.post(origin + '/v1/owner/journal/collections/favorite', {
    headers: { Origin: origin, 'X-Inky-CSRF': session.csrf },
    data: { revision: journal.revision, taxon_id: cardinal.taxon_id, favorite: false },
  });
  assert.equal(edited.status(), 200);
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('This step did not finish');
  await page.getByRole('button', { name: 'Check saved state', exact: true }).click();
  await step('5. Save your trip');
  await page.getByRole('button', { name: 'Edit trip selection', exact: true }).click();
  await step('4. Choose your checklists');
  assert.equal(
    await page.getByLabel('Trip name', { exact: true }).inputValue(),
    'A synthetic spring trip',
  );
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('button', { name: 'Review this trip', exact: true }).waitFor();
  assert(await page.getByRole('checkbox', { name: /2001-03-01/ }).isChecked());
  assert(!(await page.getByRole('checkbox', { name: /2001-03-02/ }).isChecked()));
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  t.phase = 'trip-save';
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('A synthetic spring trip');
  const trip = state().collections.find((group) => group.kind === 'personal-trip');
  assert.equal(trip.members.length, 1);
  assert.equal(trip.members[0].taxon_id, cardinal.taxon_id);
  assert(!fs.existsSync(path.join(fixture, 'var/controller/owner-import/upload.bin')));
  t.phase = 'portable-export';
  page.once('dialog', (dialog) => dialog.accept());
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export collection', exact: true }).click();
  const download = await downloadPromise;
  const exported = fs.readFileSync(await download.path(), 'utf8');
  assert(!exported.includes('checklist_hash'));
  assert(!exported.includes('Fixture garden'));
  const portablePath = path.join(fixture, 'portable.json');
  fs.writeFileSync(portablePath, exported, { mode: 0o600 });
  t.phase = 'portable-import-response-loss';
  await page.goto(origin + '/app?page=import-collection');
  await heading('Import a collection');
  await page.getByLabel('Inky collection file', { exact: true }).setInputFiles(portablePath);
  await page.getByRole('button', { name: 'Import this selection', exact: true }).waitFor();
  let interrupted = false;
  await context.route('**/v1/owner/journal/collections/import', async (route) => {
    if (!interrupted) {
      interrupted = true;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole('button', { name: 'Import this selection', exact: true }).click();
  await page.getByText(/You can retry this same save safely/).waitFor();
  const after = state().collections.length;
  await page.getByRole('button', { name: 'Import this selection', exact: true }).click();
  await step('A synthetic spring trip');
  assert.equal(state().collections.length, after);
  assert.equal(
    state().collections.filter((group) => group.kind === 'imported-selection').length,
    1,
  );
  const imported = state().collections.find((group) => group.kind === 'imported-selection');
  await page.waitForURL((url) => url.searchParams.get('view') === imported.collection_id);
  t.phase = 'retained-history';
  await page.goto(origin + '/app?page=imports&kind=ebird-archive');
  await heading('Your eBird history');
  await page
    .getByRole('button', { name: 'Use retained history without an upload', exact: true })
    .click();
  await step('4. Choose your checklists');
  await page.getByLabel('Trip name', { exact: true }).fill('Missing Robin fixture');
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('checkbox', { name: /2001-03-02/ }).check();
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('Missing Robin fixture');
  t.phase = 'explicit-missing-plate-queue';
  await page.getByRole('button', { name: 'Review missing-plate requests', exact: true }).click();
  await heading('Queue missing plates?');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/generation-queue.json')));
  await page.getByRole('button', { name: 'Confirm these plate requests', exact: true }).click();
  await heading('Operation outcome');
  await shown(page, 'Finished');
  // Every count the request returns stays on the outcome.
  await shown(page, '1 plate request saved');
  await shown(page, '0 birds still held');
  const queue = JSON.parse(
    fs.readFileSync(path.join(fixture, 'var/controller/generation-queue.json')),
  ).species;
  assert.equal(queue.length, 1);
  assert.equal(queue[0].taxon_id, robin.taxon_id);
  assert.deepEqual(queue[0].sources, ['owner-selection']);
  Object.assign(t, { trip, imported });
}

async function collectionFilter(t) {
  const { page, origin, heading } = t;
  t.phase = 'collection-filter';
  await page.goto(origin + '/app?page=edit-collection');
  await heading('New collection');
  await page.getByLabel('Collection name', { exact: true }).fill('Filter journey');
  await page.getByRole('checkbox', { name: /Northern Cardinal/ }).check();
  // Filtering hides non-matching rows (role queries exclude hidden nodes)
  // while chosen birds stay chosen. Scientific names match too.
  await page.getByLabel('Find a bird to add', { exact: true }).fill('Turdus');
  // Enter in the search box finishes the search; it does not save.
  await page.getByLabel('Find a bird to add', { exact: true }).press('Enter');
  await page.getByRole('checkbox', { name: /American Robin/ }).waitFor();
  await heading('New collection');
  assert.equal(await page.getByRole('checkbox', { name: /Northern Cardinal/ }).count(), 0);
  await page.getByRole('checkbox', { name: /American Robin/ }).check();
  await page.getByLabel('Find a bird to add', { exact: true }).fill('zz-no-such-bird');
  await page.getByText('0 species shown · chosen birds stay chosen', { exact: true }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: /American Robin/ }).count(), 0);
  await page.getByLabel('Find a bird to add', { exact: true }).fill('');
  assert(await page.getByRole('checkbox', { name: /Northern Cardinal/ }).isChecked());
  assert(await page.getByRole('checkbox', { name: /American Robin/ }).isChecked());
  // Leaving without saving commits nothing.
  await page.goto(origin + '/app?page=collections');
  await heading('Collections');
  assert.equal(await page.getByRole('link', { name: 'Filter journey', exact: true }).count(), 0);
  await page.getByText('1 plate ready · 0 not ready', { exact: true }).first().waitFor();
  assert.equal(await page.getByText(/1 plates ready/).count(), 0);
  await page.goto(origin + '/app?page=edit-collection');
  await heading('New collection');
  await page.getByLabel('Collection name', { exact: true }).fill('Filter journey');
  await page.getByRole('checkbox', { name: /Northern Cardinal/ }).check();
  await page.getByLabel('Find a bird to add', { exact: true }).fill('American Robin');
  await page.getByRole('checkbox', { name: /American Robin/ }).check();
  await page.getByRole('button', { name: 'Save collection', exact: true }).click();
  await heading('Collections');
  await page.getByRole('link', { name: 'Filter journey', exact: true }).click();
  await heading('Filter journey');
  await page.getByRole('link', { name: /Northern Cardinal/ }).first().waitFor();
  await page.getByRole('link', { name: /American Robin/ }).first().waitFor();
  // On a phone the collection shows two plates side by side, like the birds list.
  const viewport = page.viewportSize();
  await page.setViewportSize({ width: 375, height: 812 });
  const cards = page.locator('.bird-grid a.bird-card');
  const [first, second] = [await cards.nth(0).boundingBox(), await cards.nth(1).boundingBox()];
  assert.equal(Math.round(first.y), Math.round(second.y));
  assert(second.x >= first.x + first.width);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize(viewport);
  // Remove the journey collection so later phases see the expected state.
  // The Remove action lives in the collection page's tools.
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Remove collection', exact: true }).click();
  await heading('Collections');
  assert.equal(await page.getByRole('link', { name: 'Filter journey', exact: true }).count(), 0);
  await unresolvedMembers(t);
}

// An imported name that matched no bird can be removed, or resolved to the
// bird with the same scientific name.
async function unresolvedMembers(t) {
  const { page, context, origin, heading, state, cardinal } = t;
  t.phase = 'unresolved-members';
  const session = (await (await context.request.get(origin + '/v1/owner/session')).json()).data;
  const post = async (route, data) => {
    const response = await context.request.post(origin + route, {
      headers: { Origin: origin, 'X-Inky-CSRF': session.csrf },
      data,
    });
    assert.equal(response.status(), 200, route);
    return (await response.json()).data;
  };
  const document = {
    format: 'inky-collection',
    schema_version: 1,
    label: 'Unresolved journey',
    start_date: null,
    end_date: null,
    members: [
      {
        taxon_id: 990001,
        common_name: '',
        scientific_name: cardinal.scientific_name,
        origin: 'saved-selection',
      },
      {
        taxon_id: 990002,
        common_name: '',
        scientific_name: 'Synthetica nonexistens',
        origin: 'saved-selection',
      },
    ],
  };
  const preview = await post('/v1/owner/journal/collections/import-preview', { document });
  assert.equal(preview.unresolved.length, 2);
  const collection_id = 'e'.repeat(32);
  await post('/v1/owner/journal/collections/import', {
    document,
    revision: state().revision,
    preview_hash: preview.preview_hash,
    collection_id,
  });
  await page.goto(origin + '/app?page=edit-collection&view=' + collection_id);
  await heading('Edit collection');
  await page
    .getByRole('checkbox', { name: cardinal.scientific_name + ' · matches Northern Cardinal' })
    .uncheck();
  await page.getByRole('checkbox', { name: 'Synthetica nonexistens', exact: true }).uncheck();
  await page.getByRole('checkbox', { name: /^Northern Cardinal/ }).check();
  await page.getByRole('button', { name: 'Save collection', exact: true }).click();
  await heading('Collections');
  const group = state().collections.find((item) => item.collection_id === collection_id);
  assert.deepEqual(
    group.members.map((member) => [member.taxon_id, member.scientific_name]),
    [[cardinal.taxon_id, cardinal.scientific_name]],
  );
  await post('/v1/owner/journal/collections/remove', { revision: state().revision, collection_id });
}

async function community(t) {
  const { page, context, origin, heading, state, cardinal, replacement } = t;
  t.phase = 'community-wizard';
  await page.goto(origin + '/app?page=community');
  await heading('Birds reported near a place');
  await page.getByRole('button', { name: 'New community search', exact: true }).click();
  for (const [label, value] of [
    ['Collection name', 'Synthetic community selection'],
    ['Start date', '2001-03-01'],
    ['End date', '2001-03-02'],
    ['Latitude', '0.125'],
    ['Longitude', '0.25'],
    ['Radius in km', '10'],
  ])
    await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: 'Check community reports', exact: true }).click();
  await heading('Operation outcome');
  await shown(page, 'Finished');
  await page.getByRole('link', { name: 'Review community selection', exact: true }).click();
  await heading('Synthetic community selection');
  assert(!state().collections.some((group) => group.kind === 'community-history'));
  await page.getByRole('button', { name: 'Save community collection', exact: true }).click();
  await heading('Operation outcome');
  await shown(page, 'Finished');
  const communityGroup = state().collections.find((group) => group.kind === 'community-history');
  assert.equal(communityGroup.members[0].origin, 'community-report');
  assert.deepEqual(communityGroup.members[0].evidence, []);
  t.phase = 'manual-community-member-label';
  const communitySession = (
    await (await context.request.get(origin + '/v1/owner/session')).json()
  ).data;
  const manualAddition = await context.request.post(
    origin + '/v1/owner/journal/collections/save',
    {
      headers: { Origin: origin, 'X-Inky-CSRF': communitySession.csrf },
      data: {
        revision: state().revision,
        collection_id: communityGroup.collection_id,
        label: communityGroup.label,
        taxon_ids: [cardinal.taxon_id, replacement.taxon_id],
      },
    },
  );
  assert.equal(manualAddition.status(), 200);
  await page.goto(origin + '/app?page=collection&view=' + communityGroup.collection_id);
  await heading('Synthetic community selection');
  assert.equal(
    await page.getByText('Community report · not your personal sighting', { exact: true }).count(),
    1,
  );
  assert.equal(
    await page.getByText('Manual selection · no community report linked', { exact: true }).count(),
    1,
  );
}

async function savedBirds(t) {
  const { page, origin, fixture, heading, cardinal } = t;
  const stored = path.join(fixture, 'var/controller/collection.json');
  const saved = () =>
    fs.existsSync(stored)
      ? JSON.parse(fs.readFileSync(stored)).taxa.map((bird) => bird.taxon_id)
      : [];
  // One tap saves a bird in place and Undo takes it back, on a phone and a desktop.
  for (const [width, height] of [
    [375, 812],
    [1280, 800],
  ]) {
    t.phase = 'saved-birds-toggle-' + width;
    await page.setViewportSize({ width, height });
    await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
    await heading('Northern Cardinal');
    assert.deepEqual(saved(), []);
    const url = page.url();
    await page.getByRole('button', { name: 'Add to Saved birds', exact: true }).click();
    await shown(page, 'Added to Saved birds.');
    assert.equal(page.url(), url);
    assert.deepEqual(saved(), [cardinal.taxon_id]);
    await page.getByRole('button', { name: 'Remove from Saved birds', exact: true }).waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await shown(page, 'Removed from Saved birds.');
    assert.deepEqual(saved(), []);
    assert.equal(await page.getByRole('button', { name: 'Undo', exact: true }).count(), 0);
    await page.getByRole('button', { name: 'Add to Saved birds', exact: true }).waitFor();
  }
  t.phase = 'saved-birds-outcome-from-manage';
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
  await heading('Northern Cardinal');
  await page.getByRole('button', { name: 'Add to Saved birds', exact: true }).click();
  await shown(page, 'Added to Saved birds.');
  assert.deepEqual(saved(), [cardinal.taxon_id]);
  await page.goto(origin + '/app?page=manage');
  await heading('Manage');
  await page.getByRole('link', { name: /^Sources/ }).click();
  await heading('Sources');
  await page
    .getByRole('link', { name: 'Change Saved birds · Finished', exact: true })
    .first()
    .click();
  await heading('Operation outcome');
  await shown(page, '1 bird added');
  const outcome = await page.locator('main').innerText();
  assert(!/[0-9a-f]{16}/.test(outcome) && !outcome.includes('_'));
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
}

// Runs only when INKY_QA_UPLOAD_FILE names a real export.
async function largeUpload(t) {
  const { page, origin, fixture, heading, step } = t;
  if (process.env.INKY_QA_UPLOAD_FILE) {
    t.phase = 'large-mobile-upload';
    await page.goto(origin + '/app?page=imports&kind=ebird-archive');
    await heading('Your eBird history');
    await page
      .getByLabel('History file', { exact: true })
      .setInputFiles(process.env.INKY_QA_UPLOAD_FILE);
    await page.getByRole('button', { name: 'Upload for checking', exact: true }).click();
    await step('2. Check the file');
    assert.equal(
      fs.statSync(path.join(fixture, 'var/controller/owner-import/upload.bin')).size,
      fs.statSync(process.env.INKY_QA_UPLOAD_FILE).size,
    );
    await page.getByRole('button', { name: 'Cancel preview', exact: true }).click();
    await heading('1. Choose the complete source file');
    assert(!fs.existsSync(path.join(fixture, 'var/controller/owner-import/upload.bin')));
  }
}

module.exports = { historyAndCollections, collectionFilter, community, savedBirds, largeUpload };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
