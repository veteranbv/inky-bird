// Manage, Sources, Settings, Plate work and candidate review.
// Run through journal.e2e.cjs only: phases run in its order and hand state
// to later phases through t.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

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

// Plate work: candidate review and recovery holds.
async function candidates(t) {
  const { page, origin, fixture, heading } = t;
  t.phase = 'candidate-inspection-and-approval';
  await page.goto(origin + '/app?page=work');
  await heading('Plate work');
  await page
    .getByRole('link', { name: 'Inspect Synthetic QA candidate 9999991', exact: true })
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
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
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
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
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
  await page.getByRole('link', { name: '1 plate held for recovery', exact: true }).click();
  await heading('Plate work');
  await page
    .getByText('Taxon 9999993: incomplete or unsafe pending state. Use operator recovery before retrying.', { exact: true })
    .waitFor();
  assert.equal(await page.getByRole('link', { name: /Inspect .*9999993/ }).count(), 0);
}

async function integrations(t) {
  const { page, context, origin, fixture, heading, trip, imported, screenContext } = t;
  t.phase = 'scoped-integrations';
  await page.goto(origin + '/app?page=integrations');
  await heading('Integrations');
  await page
    .getByLabel('Permitted view', { exact: true })
    .first()
    .selectOption(imported.collection_id);
  await page
    .getByLabel(
      'I approve exposing this selected species membership on my trusted network or VPN',
      { exact: true },
    )
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Enable this art view', exact: true }).click(),
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
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'I saved it · clear this copy', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(await page.getByText(serviceSecret, { exact: true }).count(), 0);
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Revoke Dashboard status', exact: true }).click(),
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
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
}

module.exports = { birdBuddySettings, candidates, integrations, health };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
