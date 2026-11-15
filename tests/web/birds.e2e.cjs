// Home, Birds and bird pages.
// Run through journal.e2e.cjs only: phases run in its order and hand state
// to later phases through t.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const measured = require('./e2e-layout.cjs');

async function unreadableNotice(t) {
  const { page, origin, fixture, heading } = t;
  t.phase = 'unreadable-part-notice';
  // A damaged state file degrades its part to a notice; the journal still opens.
  const queuePath = path.join(fixture, 'var/controller/generation-queue.json');
  const queueBefore = fs.existsSync(queuePath) ? fs.readFileSync(queuePath) : null;
  fs.writeFileSync(queuePath, '{ damaged');
  const queueNotice = 'The list of birds waiting for plates could not be read.';
  for (const [query, title] of [['', 'Your journal'], ['?page=birds', 'Your birds']]) {
    await page.goto(origin + '/app' + query);
    await heading(title);
    await page.getByRole('status').filter({ hasText: queueNotice }).waitFor();
  }
  if (queueBefore) fs.writeFileSync(queuePath, queueBefore);
  else fs.rmSync(queuePath);
  await page.goto(origin + '/app');
  await heading('Your journal');
  assert.equal(await page.getByText(queueNotice).count(), 0);
}

async function artDelivery(t) {
  const { page, context, origin, engine, heading } = t;
  t.phase = 'birds-art-delivery';
  // Grid cards load the small plate; Back to birds is served from the browser
  // cache. Any route disables the HTTP cache, so routing is lifted for this
  // phase only. Same-origin CSP still keeps every request local.
  await page.goto(origin + '/app?page=birds&scope=art');
  await heading('More plates');
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
  await startUp(t);
  await listReturn(t);
  await layout(t);
}

// Every file the owner page names loads, and a start that never comes says so.
async function startUp(t) {
  const { page, origin, heading } = t;
  t.phase = 'owner-page-start';
  const failed = [];
  const own = (url) => new URL(url).pathname.startsWith('/app');
  const answered = (response) => {
    if (response.status() >= 400 && own(response.url())) failed.push(response.status() + ' ' + response.url());
  };
  const lost = (request) => {
    if (own(request.url())) failed.push('failed ' + request.url());
  };
  page.on('response', answered);
  page.on('requestfailed', lost);
  await page.goto(origin + '/app');
  await heading('Your journal');
  page.off('response', answered);
  page.off('requestfailed', lost);
  assert.deepEqual(failed, []);
  assert.equal(await page.locator('.start-stalled').count(), 0, 'the started app replaces the message');
  // A module that never loads stands in for an old browser or a lost connection.
  await page.route('**/app/app.js', (route) => route.abort());
  await page.goto(origin + '/app');
  const stalled = page.locator('.start-stalled');
  assert.equal(await stalled.isVisible(), false, 'no message while the page may still start');
  await page.evaluate(() => document.getAnimations().forEach((animation) => animation.finish()));
  await stalled.waitFor({ state: 'visible' });
  await page.unroute('**/app/app.js');
}

// Every control a tap can reach, with its box, for the layout checks below.
// Links inside running text are exempt from the target size, as in WCAG 2.5.8.
function controls(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll('header a, main a, main button, main input, main select, main summary, nav a')]
      .filter((node) => node.getClientRects().length && !node.closest('p, li, dd'))
      .map((node) => {
        const box = node.getBoundingClientRect();
        return {
          name: (node.getAttribute('aria-label') || node.textContent || node.id || node.tagName).trim().slice(0, 60),
          fixed: !!node.closest('.journal-navigation'),
          box: { left: box.left, top: box.top, right: box.right, bottom: box.bottom },
        };
      }),
  );
}

async function checkLayout(page, label) {
  // Problems are collected so a failure lists every one of them.
  const problems = [];
  const wide = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, width: innerWidth }));
  if (wide.scrollWidth > wide.width) problems.push({ problem: label + ': sideways scrolling', ...wide });
  const found = await controls(page);
  for (const { name, box } of found)
    if (box.right - box.left < 44 - 0.5 || box.bottom - box.top < 44 - 0.5)
      problems.push({ problem: label + ': ' + name + ' is smaller than 44 px', box });
  // The bottom navigation floats over the page, so each layer is checked on its own.
  for (const [n, a] of found.entries())
    for (const b of found.slice(n + 1))
      if (
        a.fixed === b.fixed &&
        a.box.right > b.box.left + 0.5 &&
        b.box.right > a.box.left + 0.5 &&
        a.box.bottom > b.box.top + 0.5 &&
        b.box.bottom > a.box.top + 0.5
      )
        problems.push({ problem: label + ': ' + a.name + ' overlaps ' + b.name, boxes: [a.box, b.box] });
  // A word split across lines means its button is too narrow for the label.
  const split = await page.evaluate(() =>
    [...document.querySelectorAll('main button, main .primary-link')]
      .filter((node) => node.getClientRects().length)
      .filter((node) => {
        const step = parseFloat(getComputedStyle(node).fontSize) * 0.5;
        const texts = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        while (texts.nextNode()) {
          const text = texts.currentNode, range = document.createRange();
          let previous = null;
          for (let index = 0; index < text.length; index += 1) {
            range.setStart(text, index);
            range.setEnd(text, index + 1);
            const top = range.getBoundingClientRect().top;
            const inWord = /\S/.test(text.data[index]) && index > 0 && /\S/.test(text.data[index - 1]);
            if (inWord && previous !== null && top > previous + step) return true;
            previous = top;
          }
        }
        return false;
      })
      .map((node) => (node.getAttribute('aria-label') || node.textContent).trim().slice(0, 60)),
  );
  for (const name of split) problems.push({ problem: label + ': ' + name + ' splits a word over two lines' });
  await measured.layout(page, problems.length === 0, label + ' has layout problems', { problems });
}

const box = (locator) => locator.evaluate((node) => node.getBoundingClientRect().toJSON());

// Words the owner reads: no raw machine values, and counts that agree with one.
async function plainWords(page, label) {
  const text = await page.locator('main').innerText();
  const raw = text.match(/\b(true|false|succeeded|last-\d+-days|last-week|last-day|all-time|[a-z]+_[a-z_]+)\b/);
  assert.equal(raw, null, label + ': a raw machine value is shown: ' + raw?.[0]);
  const wrong = text.match(/\b1 (records|birds|plates|checklists)\b|\b(?:[02-9]|\d\d+) (record|bird|plate) (?!is\b)/);
  assert.equal(wrong, null, label + ': a count disagrees with its noun: ' + wrong?.[0]);
  for (const old of ['Available artwork', 'Current and saved', 'not a sighting', 'held for recovery', 'Plate work'])
    assert(!text.includes(old), label + ': old name ' + old);
}

// Lately, the journal size and the reason the featured plate is shown.
async function journalHeart(page, size) {
  const lately = page.locator('.lately');
  await lately.getByRole('heading', { name: 'Lately', exact: true }).waitFor();
  const entries = lately.locator('a.lately-bird');
  const count = await entries.count();
  assert(count >= 1 && count <= 3, size + ': Lately shows one to three birds');
  // The newest record in the synthetic history is the robin's.
  assert.equal(await entries.first().getAttribute('aria-label'), null);
  assert.equal((await entries.first().textContent()).replace(/\s+/g, ' ').trim(), 'American Robin 2 Mar 2001');
  assert.match(new URL(await entries.first().getAttribute('href'), 'https://inky.example.test').search, /page=bird&bird=\d+/);
  const sizeLine = await page.locator('.journal-size').innerText();
  assert.match(sizeLine, /^\d+ birds? in your journal( · (1 with a plate|\d+ with plates))?$/, size);
  const why = page.locator('.why-this-bird');
  if (await why.count())
    assert.match(await why.innerText(), /^(Noted( (at your feeder|on your microphone|in your checklists))? this week|Seen nearby this week|From your journal, \d{4})$/);
  await plainWords(page, 'Home ' + size);
  for (const [a, b] of [['.home-feature', '.lately']]) {
    const [first, second] = [await box(page.locator(a)), await box(page.locator(b))];
    await measured.layout(page, second.top >= first.bottom - 0.5, size + ': Lately overlaps the plate', { first, second });
  }
}

// Birds list, bird page and Home at phone, small-phone and desktop sizes.
async function layout(t) {
  const { page, origin, heading, cardinal } = t;
  const check = (ok, what, boxes) => measured.layout(page, ok, what, boxes);
  for (const [width, height] of [
    [375, 812],
    [320, 568],
    [1280, 800],
  ]) {
    const size = width + 'x' + height;
    await page.setViewportSize({ width, height });
    t.phase = 'birds-layout-' + size;
    await page.goto(origin + '/app?page=birds');
    await heading('Your birds');
    await checkLayout(page, 'Birds ' + size);
    await plainWords(page, 'Birds ' + size);
    const cards = page.locator('.bird-grid a.bird-card');
    assert(await cards.count() >= 2, 'the list has at least two birds to lay out');
    const [first, second] = [await box(cards.nth(0)), await box(cards.nth(1))];
    if (width < 560) {
      await check(Math.round(first.top) === Math.round(second.top), size + ': two cards not side by side', { first, second });
      await check(second.left > first.right, size + ': not two columns', { first, second });
    }
    const plate = await box(page.locator('.bird-grid img').first());
    const room = await page.locator('.bird-grid img').first().evaluate((image) => {
      const card = image.parentElement, style = getComputedStyle(card);
      return card.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    });
    await check(plate.width >= room - 0.5, size + ': the plate does not fill its card', { plate, room });
    if (width === 375) await check(plate.top < height, size + ': the first plate starts below the first screen', { plate });
    if (width >= 820) {
      const search = await box(page.locator('#bird-search'));
      await check(search.width <= 420.5, size + ': search is wider than a readable width', { search });
    }

    t.phase = 'bird-layout-' + size;
    await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
    await heading('Northern Cardinal');
    await page.locator('.detail-art').evaluate((image) => image.decode());
    await checkLayout(page, 'Bird ' + size);
    // The synthetic eBird history holds one checklist for the cardinal.
    await page
      .getByText('In your journal: noted 1 Mar 2001 · 1 record from your checklists', { exact: true })
      .waitFor();
    await plainWords(page, 'Bird ' + size);
    const back = await box(page.getByRole('link', { name: 'Back to birds', exact: true }));
    const title = await box(page.getByRole('heading', { name: 'Northern Cardinal', exact: true }));
    await check(back.bottom <= title.top + 0.5, size + ': Back does not come before the title', { back, title });
    const [left, right] = [await box(page.locator('.bird-plate')), await box(page.locator('.bird-notes'))];
    if (width >= 960) {
      await check(left.right <= right.left, size + ': notes do not sit beside the plate', { left, right });
      const favorites = await box(page.getByRole('button', { name: /Favorites$/ }));
      await check(favorites.width <= 420.5, size + ': buttons are wider than a readable width', { favorites });
    } else await check(right.top >= left.bottom - 0.5, size + ': notes do not follow the plate', { left, right });

    // Collections keep one card to a row on phones; only bird lists pair up.
    t.phase = 'collections-layout-' + size;
    await page.goto(origin + '/app?page=collections');
    await heading('Collections');
    await checkLayout(page, 'Collections ' + size);
    if (width < 560) {
      const groups = page.locator('.bird-grid > .journal-card');
      assert(await groups.count() >= 2, 'Collections lists at least two groups');
      const [one, two] = [await box(groups.nth(0)), await box(groups.nth(1))];
      await check(two.top >= one.bottom, size + ': more than one collection to a row', { one, two });
    }

    t.phase = 'home-layout-' + size;
    await page.goto(origin + '/app');
    await heading('Your journal');
    await checkLayout(page, 'Home ' + size);
    await journalHeart(page, size);
    if (width >= 960) {
      const [hero, rail] = [await box(page.locator('.featured-art')), await box(page.locator('.home-rail'))];
      await check(hero.right <= rail.left, size + ': the caption does not sit beside the plate', { hero, rail });
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
}

// Search, sort and the opened card come back with Back; the bird page steps
// through the same list.
async function listReturn(t) {
  const { page, origin, heading, cardinal } = t;
  t.phase = 'birds-list-return';
  await page.goto(origin + '/app?page=birds&scope=art');
  await heading('More plates');
  await page.getByLabel('Sort by', { exact: true }).selectOption('scientific');
  await page.getByLabel('Find a bird', { exact: true }).fill('cardinal');
  const address = new URLSearchParams(await page.evaluate(() => location.search));
  assert.equal(address.get('q'), 'cardinal', address.toString());
  assert.equal(address.get('sort'), 'scientific');
  await page.locator('.bird-grid a.bird-card:visible').first().click();
  await heading('Northern Cardinal');
  await page.goBack();
  await heading('More plates');
  assert.equal(await page.getByLabel('Find a bird', { exact: true }).inputValue(), 'cardinal');
  assert.equal(await page.getByLabel('Sort by', { exact: true }).inputValue(), 'scientific');
  assert.equal(
    await page.evaluate(() => document.activeElement?.getAttribute('href')),
    '/app?page=bird&bird=' + cardinal.taxon_id + '&scope=art&q=cardinal&sort=scientific',
  );
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id + '&scope=art');
  await heading('Northern Cardinal');
  const steps = await page.locator('.bird-nav a').evaluateAll((links) => links.map((node) => node.textContent));
  assert.equal(steps[0], 'Back to birds');
  assert.equal(await page.locator('.plate-link').getAttribute('href'), await page.locator('.detail-art').getAttribute('src'));
}

module.exports = { unreadableNotice, artDelivery };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
