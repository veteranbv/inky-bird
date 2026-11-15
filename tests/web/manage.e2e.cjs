// Manage, Sources, Settings, In the works and new plates.
// Run through journal.e2e.cjs only: phases run in its order and hand state
// to later phases through t.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { noSideways } = require('./e2e-layout.cjs');
const { execFileSync } = require('node:child_process');

async function birdBuddySettings(t) {
  const { page, origin, heading } = t;
  t.phase = 'birdbuddy-bootstrap-ux';
  await page.goto(origin + '/app?page=settings&source=birdbuddy');
  await heading('Bird Buddy');
  assert(await page.getByLabel('Dedicated guest email', { exact: true }).isDisabled());
  const permission = page.getByRole('checkbox', {
    name: 'I confirm that Bird Buddy has authorized automated API access for this account.',
    exact: true,
  });
  await permission.check();
  await page
    .getByLabel('Dedicated guest email', { exact: true })
    .fill('synthetic-guest@example.test');
  await page
    .getByLabel('Guest password (used only for this sign-in)', { exact: true })
    .fill('SYNTHETIC_GUEST_NOT_SENT');
  await permission.uncheck();
  assert.equal(await page.getByLabel('Dedicated guest email', { exact: true }).inputValue(), '');
  assert.equal(
    await page
      .getByLabel('Guest password (used only for this sign-in)', { exact: true })
      .inputValue(),
    '',
  );
}

// Saving a key from a session signed in long ago asks for the password first;
// typing it must not withdraw the review that holds the confirmation.
async function recentSignIn(t) {
  const { page, origin, root, fixture, heading, password } = t;
  t.phase = 'settings-recent-sign-in';
  // Age this installation's own sessions past the recent sign-in window,
  // under the store's lock, as real time would.
  execFileSync(
    process.env.INKY_QA_PYTHON || path.join(root, '.venv/bin/python'),
    [
      '-c',
      `from dataclasses import replace
from pathlib import Path
from inky_bird_frame.access import RECENT_AUTH_SECONDS, AccessStore
from inky_bird_frame.config import load_config
store=AccessStore(load_config(Path(${JSON.stringify(path.join(fixture, 'config.toml'))}),load_secrets=False).controller.state_dir)
with store._locked() as state:
 assert state.sessions
 state.sessions=[replace(s,authenticated_at=s.authenticated_at-2*RECENT_AUTH_SECONDS) for s in state.sessions]
 store._save(state)
`,
    ],
    { cwd: root, env: { PATH: process.env.PATH, PYTHONPATH: root + '/src' } },
  );
  await page.goto(origin + '/app?page=settings&source=ebird');
  await heading('eBird nearby');
  await page.getByLabel('eBird API key', { exact: true }).fill('SYNTHETIC_EBIRD_KEY');
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  const confirmation = page.getByLabel('Your Inky password', { exact: true });
  await confirmation.waitFor();
  // Typed key by key, as an owner would.
  await confirmation.pressSequentially(password);
  await confirmation.press('Enter');
  await page.getByRole('button', { name: 'Confirm and save', exact: true }).click();
  await heading('eBird nearby');
  await page.getByText('Saved. Leave this empty to keep it.', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('eBird API key', { exact: true }).inputValue(), '');
}

// In the works: new plates and plates that need a hand.
async function candidates(t) {
  const { page, origin, fixture, heading } = t;
  t.phase = 'candidate-inspection-and-approval';
  await page.goto(origin + '/app?page=work');
  await heading('In the works');
  await page.getByText('Plates are made one at a time.', { exact: true }).waitFor();
  await page.getByText('2 new plates are ready to look at.', { exact: true }).waitFor();
  await page.getByRole('heading', { name: 'Needs a hand', exact: true }).waitFor();
  const workText = await page.locator('main').innerText();
  assert(!/Plate work|Runnable|held for recovery|operator recovery|succeeded|_/.test(workText), 'machine words on In the works');
  await noSideways(page, 'In the works');
  await page
    .getByRole('link', { name: 'Look at Synthetic QA candidate 9999991', exact: true })
    .click();
  await heading('Review candidate');
  await page.locator('.detail-art').evaluate((image) => image.decode());
  await page
    .getByRole('checkbox', {
      name: 'I inspected the full plate, its text, anatomy and cited review. Make this artwork approved locally and eligible for existing publication.',
      exact: true,
    })
    .check();
  await page.getByRole('button', { name: 'Confirm local approval', exact: true }).click();
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('The plate is in your journal.', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('The plate is in your journal.', { exact: true }).waitFor();
  await heading('Keeping a new plate');
  assert(
    fs.existsSync(
      path.join(fixture, 'catalog/species/9999991-synthetic-qa-candidate-9999991/manifest.json'),
    ),
  );
  t.phase = 'candidate-rejection';
  await page.goto(origin + '/app?page=candidate&bird=9999992');
  await heading('Review candidate');
  await page
    .getByLabel('Why reject this candidate?', { exact: true })
    .fill('Synthetic QA rejection, retain the fixture privately.');
  await page.getByRole('button', { name: 'Confirm rejection', exact: true }).click();
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('The plate was sent back.', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('The plate was sent back.', { exact: true }).waitFor();
  await heading('Sending a plate back');
  assert(
    fs.existsSync(
      path.join(
        fixture,
        'var/controller/rejected/9999992-synthetic-qa-candidate-9999992/manifest.json',
      ),
    ),
  );
  t.phase = 'recovery-hold-banner';
  await page.goto(origin + '/app');
  await heading('Your field journal');
  await page.getByRole('link', { name: '1 plate held for recovery', exact: true }).waitFor();
  assert.equal(await page.getByText(/ready for your review/).count(), 0);
  // Manage counts the plate that needs a hand and links to it.
  await page.goto(origin + '/app?page=manage');
  await heading('Manage');
  await page.getByRole('link', { name: /^(1 plate needs|[2-9] plates need) a hand$/ }).click();
  await heading('In the works');
  await page
    .getByText(/^Taxon 9999993: its new plate is unfinished\./)
    .waitFor();
  assert.equal(await page.getByRole('link', { name: /Look at .*9999993/ }).count(), 0);
}

async function integrations(t) {
  const { page, context, origin, fixture, heading, trip, imported, screenContext } = t;
  t.phase = 'scoped-integrations';
  await page.goto(origin + '/app?page=integrations');
  await heading('Integrations');
  await page
    .getByLabel('Birds to show', { exact: true })
    .first()
    .selectOption(imported.collection_id);
  await page
    .getByLabel(
      'I understand anyone on my network or VPN can see which birds this shows',
      { exact: true },
    )
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Turn on dashboard plates', exact: true }).click(),
  ]);
  const artResponse = await context.request.get(origin + '/v1/dashboard/art');
  assert.equal(artResponse.status(), 200);
  const artText = await artResponse.text();
  assert(!artText.includes(trip.label));
  assert(!artText.includes('2001-03-01'));
  await page.getByRole('button', { name: 'Create status credential', exact: true }).click();
  await page.getByRole('heading', { name: 'Copy this credential now', exact: true }).waitFor();
  const serviceSecret = await page.locator('.notice pre').first().innerText();
  const aggregate = await context.request.get(origin + '/v1/integrations/status', {
    headers: { Authorization: 'Bearer ' + serviceSecret },
  });
  assert.equal(aggregate.status(), 200);
  assert(!JSON.stringify(await aggregate.json()).includes(trip.label));
  await noSideways(page, 'Integrations');
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'I saved it · clear this copy', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(await page.getByText(serviceSecret, { exact: true }).count(), 0);
  await page.getByRole('button', { name: 'Revoke Dashboard status', exact: true }).click();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Revoke', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(
    (
      await context.request.get(origin + '/v1/integrations/status', {
        headers: { Authorization: 'Bearer ' + serviceSecret },
      })
    ).status(),
    401,
  );
  await screenContext.close();
  for (const filename of ['collection.json', 'display-last-success.json'])
    assert(!fs.existsSync(path.join(fixture, 'var/controller', filename)));
}

async function health(t) {
  const { page, origin, heading } = t;
  t.phase = 'health-and-guides';
  await page.goto(origin + '/app?page=health');
  await heading('Health and help');
  await page.getByRole('heading', { name: 'Approved catalog index · ok', exact: true }).waitFor();
  for (const [area, title] of [
    ['backups', 'Backups and upgrades'],
    ['advanced', 'Advanced tools'],
  ]) {
    await page.goto(origin + '/app?page=' + area);
    await heading(title);
    await noSideways(page, title);
  }
}

module.exports = { birdBuddySettings, recentSignIn, candidates, integrations, health };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
