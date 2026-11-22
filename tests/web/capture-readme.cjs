// Capture real app pages using public catalog art and a disposable local owner.
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { mkdirSync, mkdtempSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.INKY_QA_PLAYWRIGHT || 'playwright');
const { startFixture } = require('./demo-fixture.cjs');

const options = process.argv.slice(2);
assert(options.filter((value) => !value.startsWith('--')).length <= 1);
assert(options.filter((value) => value.startsWith('--'))
  .every((value) => ['--export-sample'].includes(value)));
const output = path.resolve(options.find((value) => !value.startsWith('--')) ||
  mkdtempSync(path.join(tmpdir(), 'inky-images-')));
mkdirSync(output, { recursive: true });
const fixture = startFixture([9083, 12727]);
const server = fixture.server;

async function capture() {
  const origin = await fixture.origin();
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-background-networking', '--use-mock-keychain'],
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 430, height: 1200 }, isMobile: true, hasTouch: true,
      reducedMotion: 'reduce', colorScheme: 'light', serviceWorkers: 'block',
    });
    await context.route('**/*', (route) =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
    );
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const password = randomBytes(32).toString('base64url');
    await page.goto(origin + '/app');
    await page.getByLabel('Username', { exact: true }).fill('demo-owner');
    await page.getByLabel('Choose your Inky password', { exact: true }).fill(password);
    await page.getByLabel('Confirm your password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await page.getByRole('heading', { name: 'Your journal', exact: true }).waitFor();

    await page.goto(origin + '/app?page=edit-collection');
    await page.getByLabel('Collection name', { exact: true }).fill('Garden birds');
    await page.getByRole('checkbox', { name: /Northern Cardinal/ }).check();
    await page.getByRole('checkbox', { name: /American Robin/ }).check();
    await page.getByRole('button', { name: 'Save collection', exact: true }).click();
    await page.getByRole('link', { name: 'Garden birds', exact: true }).click();
    await page.getByRole('heading', { name: 'Garden birds', exact: true }).waitFor();

    async function image(name) {
      await page.evaluate(async () => {
        await document.fonts.ready;
        for (const image of document.querySelectorAll('main img')) await image.decode();
      });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, name), scale: 'css', animations: 'disabled' });
    }
    await page.evaluate(() => scrollTo(0, 0));
    await image('journal-collection.png');
    await page.goto(origin + '/app?page=bird&bird=9083');
    await page.getByRole('button', { name: 'Add to my frame', exact: true }).click();
    await page.getByText('Added to On my frame.', { exact: true }).waitFor({ timeout: 30000 });
    await page.goto(origin + '/app');
    await page.getByRole('heading', { name: 'Your journal', exact: true }).waitFor();
    // Home shows Play twice once a plate is on the frame: the Journal
    // navigation tab and the primary link beside the current plate.
    await page.getByRole('navigation', { name: 'Journal', exact: true })
      .getByRole('link', { name: 'Play', exact: true }).waitFor();
    await image('journal-home.png');
    if (process.argv.includes('--export-sample')) {
      const sample = await page.evaluate(async () => {
        const response = await fetch('/v1/owner/journal', { credentials: 'same-origin' });
        if (!response.ok) throw new Error('Synthetic journal export failed');
        return (await response.json()).data;
      });
      writeFileSync(path.join(output, 'sample-journal.json'), JSON.stringify(sample, null, 2) + '\n');
    }
    assert.deepEqual(errors, []);
    console.log(`Screenshots: ${output}`);
  } finally {
    await browser.close();
  }
}

capture().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
}).finally(() => server.kill());
