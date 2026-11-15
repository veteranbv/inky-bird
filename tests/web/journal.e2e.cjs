// Owner journal browser acceptance: one synthetic installation, every area's
// journeys in one ordered pass. Each area keeps its journeys in its own file.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const { run } = require('./e2e-harness.cjs');
const birds = require('./birds.e2e.cjs');
const save = require('./save.e2e.cjs');
const frame = require('./frame.e2e.cjs');
const manage = require('./manage.e2e.cjs');

async function ownerSetup(t) {
  const { page, context, origin, fixture, heading, username, password } = t;
  t.phase = 'reserved-setup-rendering';
  const reservedStatus = async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    assert.equal(payload.data.setup_required, true);
    payload.data.setup_code = { state: 'ready', expires_in_seconds: 60 };
    await route.fulfill({ response, json: payload });
  };
  await context.route(origin + '/v1/access', reservedStatus);
  await page.goto(origin + '/app');
  await heading('Finish setup on the controller');
  assert.equal(await page.locator('input').count(), 0);
  await page.getByText('If you use Docker', {exact: true}).click();
  await page.getByText('docker compose exec controller inky-bird-frame access setup --config /data/config.toml', {exact: true}).waitFor();
  const guide = await context.request.get(origin + '/app/access-guide');
  assert.equal(guide.status(), 200);
  assert((await guide.text()).includes('An unattended upgrade does not wait for a prompt.'));
  await page.setViewportSize({width: 320, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({width: 390, height: 844});
  await context.unroute(origin + '/v1/access', reservedStatus);
  t.phase = 'registration';
  await page.reload();
  await heading('Make Inky yours');
  const cookieTrustNotice =
    'Use this address only with web apps you trust. A different port does not keep sign-in cookies separate.';
  await page.getByText(cookieTrustNotice, { exact: true }).waitFor();
  // Check the HTTPS rendering branch only; real TLS is qualified separately.
  const encryptedStatus = async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.data.encrypted = true;
    await route.fulfill({ response, json: payload });
  };
  await context.route(origin + '/v1/access', encryptedStatus);
  await page.reload();
  await page.getByText(/Encrypted connection/).waitFor();
  await page.getByText(cookieTrustNotice, { exact: true }).waitFor();
  await context.unroute(origin + '/v1/access', encryptedStatus);
  await page.reload();
  await heading('Make Inky yours');
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  fs.writeFileSync(fixture + '/sign-in-phone.png', await page.screenshot({ fullPage: true }), {
    mode: 0o600,
  });
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Choose your Inky password', { exact: true }).fill(password);
  await page.getByLabel('Confirm your password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await heading('Your field journal');
  await page
    .getByText('No sources selected for refresh. Artwork and saved collections still work.', {
      exact: true,
    })
    .waitFor();
  assert.equal(await page.getByText(/The last saved selection may be out of date/).count(), 0);
  await page.getByRole('link', { name: '2 plates ready for your review', exact: true }).waitFor();
  await page.getByRole('link', { name: '1 plate held for recovery', exact: true }).waitFor();
  assert.equal(await page.getByText(/3 plates ready for your review/).count(), 0);
}

async function providerSignIn(t) {
  const { browser, origin } = t;
  t.phase = 'sso-error-visibility';
  // Frontend contract only: the provider state and start failure are stubbed,
  // like the community query. Real OIDC exchange stays backend-tested.
  const ssoContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
  });
  const ssoPage = await ssoContext.newPage();
  await ssoPage.route('**/v1/access', (route) =>
    route.fulfill({
      json: {
        ok: true,
        schema_version: 1,
        data: {
          secure: true,
          setup_required: false,
          registration_allowed: false,
          setup_code: null,
          password_min_length: 15,
          password_max_length: 1024,
          encrypted: true,
          access_mode: 'https-only',
          legacy_account: false,
          session_policy: { regular_hours: 24, remembered_days: 30 },
          provider: { label: 'Authentik', ready: true, linked: true },
          local_login: true,
        },
      },
    }),
  );
  await ssoPage.route('**/v1/owner/oidc/start', (route) =>
    route.fulfill({
      status: 502,
      json: {
        ok: false,
        schema_version: 1,
        error: { code: 'provider_error', message: 'Synthetic provider start failure.' },
      },
    }),
  );
  await ssoPage.goto(origin + '/app');
  await ssoPage.getByRole('heading', { name: 'Welcome back', exact: true }).waitFor();
  const passwordDisclosure = ssoPage.locator('details.auth-alt');
  assert.equal(await passwordDisclosure.count(), 1);
  assert(!(await passwordDisclosure.evaluate((node) => node.open)));
  // One shared stay-signed-in control, one hint, used by the leading action.
  assert.equal(
    await ssoPage.getByRole('checkbox', { name: 'Stay signed in on this device' }).count(),
    1,
  );
  assert.equal(await ssoPage.locator('#remember-hint').count(), 1);
  const ssoStart = ssoPage.getByRole('button', { name: 'Continue with Authentik', exact: true });
  await ssoStart.click();
  await ssoPage
    .getByText('Synthetic provider start failure.', { exact: true })
    .waitFor({ state: 'visible' });
  assert(await ssoStart.isEnabled());
  assert(!(await passwordDisclosure.evaluate((node) => node.open)));
  await passwordDisclosure.locator('summary').click();
  assert(await passwordDisclosure.evaluate((node) => node.open));
  assert.equal(
    await ssoPage.getByRole('checkbox', { name: 'Stay signed in on this device' }).count(),
    1,
  );
  assert.equal(await ssoPage.locator('#remember-hint').count(), 1);
  await ssoContext.close();
}

async function finish(t) {
  const { page, origin, fixture, root, engine, heading, errors } = t;
  if (process.env.INKY_QA_AXE) {
    const { default: AxeBuilder } = require(process.env.INKY_QA_AXE),
      audits = [];
    for (const [area, title] of [
      ['home', 'Your field journal'],
      ['birds', 'Your birds'],
      ['collections', 'Collections'],
      ['manage', 'Manage'],
      ['health', 'Health and help'],
      ['integrations', 'Integrations'],
      ['displays', 'Displays'],
      ['settings', 'Settings and backups'],
    ]) {
      t.phase = 'accessibility-' + area;
      await page.goto(origin + '/app?page=' + area);
      await heading(title);
      const result = await new AxeBuilder({ page }).analyze();
      audits.push({
        page: area,
        violations: result.violations.map(({ id, impact, nodes }) => ({
          id,
          impact,
          targets: nodes.map((node) => node.target),
        })),
      });
    }
    fs.writeFileSync(fixture + '/accessibility-result.json', JSON.stringify(audits, null, 2), {
      mode: 0o600,
    });
    assert(
      audits.every((result) => result.violations.length === 0),
      'automated accessibility violations need review',
    );
  }
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    fixture + '/history-browser-result.json',
    JSON.stringify(
      {
        passed: true,
        synthetic: true,
        communityQuery: 'mocked',
        reservedSetupRendering: 'synthetic access status; rejection tested through real backend',
        engine,
        sourceCommit: execFileSync('git', ['rev-parse', 'HEAD'], {
          cwd: root,
          encoding: 'utf8',
        }).trim(),
        sourceDirty:
          execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim()
            .length > 0,
        largeUpload: !!process.env.INKY_QA_UPLOAD_FILE,
        accessibility: !!process.env.INKY_QA_AXE,
        checks: [
          'owner setup',
          'HTTP and HTTPS cookie-host trust guidance (HTTPS rendering synthetic)',
          'Favorites separate from sightings',
          'complete archive upload',
          'separate history confirmation',
          'explicit checklist selection',
          'stale edit and response-loss recovery',
          'portable collection export/import',
          'retained history',
          'explicit missing-plate requests',
          'strict candidate approval/rejection',
          'paired-screen scope withdrawal and revocation',
          'paired-screen two-step Forget with failure kept paired',
          'scoped dashboard art',
          'separate status credential and revocation',
          'community evidence not personal',
          'Health and operator guidance',
          'explicit Saved birds preview',
          'Frame viewing tools',
          'Frame wake tap, fade after tap, swipe, outage and emptied-view recovery',
          'Frame phone More bar and wide single bar',
          'no browser storage credentials',
          'no model process or physical panel write',
          'no script errors',
        ],
      },
      null,
      2,
    ),
    { mode: 0o600 },
  );
  console.log('Journal browser acceptance passed (' + engine + '). Synthetic evidence: ' + fixture);
}

run(async (t) => {
  await ownerSetup(t);
  await manage.birdBuddySettings(t);
  await save.historyAndCollections(t);
  await manage.candidates(t);
  await birds.unreadableNotice(t);
  await save.collectionFilter(t);
  await frame.pairedScreen(t);
  await manage.integrations(t);
  await save.community(t);
  await manage.health(t);
  await save.savedBirds(t);
  await birds.artDelivery(t);
  await frame.ownerFrame(t);
  await providerSignIn(t);
  await save.largeUpload(t);
  await finish(t);
});
