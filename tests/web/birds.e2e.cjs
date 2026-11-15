// Home, Birds and bird pages.
// Run through journal.e2e.cjs only: phases run in its order and hand state
// to later phases through t.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function unreadableNotice(t) {
  const { page, origin, fixture, heading } = t;
  t.phase = 'unreadable-part-notice';
  // A damaged state file degrades its part to a notice; the journal still opens.
  const queuePath = path.join(fixture, 'var/controller/generation-queue.json');
  const queueBefore = fs.existsSync(queuePath) ? fs.readFileSync(queuePath) : null;
  fs.writeFileSync(queuePath, '{ damaged');
  const queueNotice = 'The list of birds waiting for plates could not be read.';
  for (const [query, title] of [['', 'Your field journal'], ['?page=birds', 'Your birds']]) {
    await page.goto(origin + '/app' + query);
    await heading(title);
    await page.getByRole('status').filter({ hasText: queueNotice }).waitFor();
  }
  if (queueBefore) fs.writeFileSync(queuePath, queueBefore);
  else fs.rmSync(queuePath);
  await page.goto(origin + '/app');
  await heading('Your field journal');
  assert.equal(await page.getByText(queueNotice).count(), 0);
}

async function artDelivery(t) {
  const { page, context, origin, engine, heading } = t;
  t.phase = 'birds-art-delivery';
  // Grid cards load the small plate; Back to birds is served from the browser
  // cache. Any route disables the HTTP cache, so routing is lifted for this
  // phase only. Same-origin CSP still keeps every request local.
  await page.goto(origin + '/app?page=birds&scope=art');
  await heading('Available artwork');
  const gridImages = page.locator('.bird-grid img');
  assert(await gridImages.count() > 0);
  const gridSources = await gridImages.evaluateAll((images) => images.map((image) => image.src));
  for (const source of gridSources)
    assert.match(source, /\/v1\/owner\/journal\/birds\/\d+\/thumbnail\?revision=[0-9a-f]{64}$/);
  const thumbnail = await page.request.get(gridSources[0]);
  assert.equal(thumbnail.headers()['cache-control'], 'private, max-age=31536000, immutable');
  assert(['image/jpeg', 'image/webp'].includes(thumbnail.headers()['content-type']));
  if (engine === 'chromium') {
    // Earlier phases are finished with their routes; only the guard returns.
    await context.unrouteAll({ behavior: 'wait' });
    const network = await page.context().newCDPSession(page);
    await network.send('Network.enable');
    const fetched = new Map();
    network.on('Network.requestWillBeSent', ({ requestId, request }) => {
      if (/\/v1\/owner\/journal\/birds\/\d+\/(thumbnail|portrait)\?/.test(request.url))
        fetched.set(requestId, request.url);
    });
    network.on('Network.requestServedFromCache', ({ requestId }) => fetched.delete(requestId));
    // Lazy images below the fold stay unloaded; only on-screen cards must be ready.
    const visibleLoaded = () =>
      page.waitForFunction(() =>
        [...document.querySelectorAll('.bird-grid img')]
          .filter((image) => {
            const box = image.getBoundingClientRect();
            return box.bottom > 0 && box.top < innerHeight;
          })
          .every((image) => image.complete && image.naturalWidth > 0),
      );
    await page.goto(origin + '/app?page=birds');
    await heading('Your birds');
    assert(await gridImages.count() > 0);
    await visibleLoaded();
    await page.locator('.bird-grid a.bird-card').first().click();
    await page.locator('.detail-art').evaluate((image) => image.decode());
    fetched.clear();
    await page.getByRole('link', { name: 'Back to birds', exact: true }).click();
    await heading('Your birds');
    await visibleLoaded();
    assert.deepEqual([...fetched.values()], [], 'Back to birds makes no new image requests');
    await network.detach();
    await context.route('**/*', (route) =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
    );
  }
}

module.exports = { unreadableNotice, artDelivery };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
