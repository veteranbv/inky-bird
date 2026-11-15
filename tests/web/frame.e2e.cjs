// Play, the wall frame and paired screens.
// Run through journal.e2e.cjs only: phases run in its order and hand state
// to later phases through t. pairedScreen leaves t.screenContext and
// t.replacement, which save and manage phases read.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { layout, noSideways, boxes } = require('./e2e-layout.cjs');

// Frame controls fade after five idle seconds and take no taps while faded,
// so every Frame step reveals them first. Narrow screens keep secondary
// controls behind More; wide screens have no More button.
const openMore = async (target) => {
  await target.keyboard.press('Shift');
  await target.locator('.frame-controls:not(.quiet)').waitFor();
  const more = target.getByRole('button', { name: 'More', exact: true });
  if ((await more.isVisible()) && (await more.getAttribute('aria-expanded')) !== 'true')
    await more.click();
};
// The browser enters fullscreen some time after the click, so wait for the
// request to settle before deciding; a late entry would otherwise leave the
// window fullscreen for every later phase.
const fullscreenSettles = async (target, entered) => {
  for (let n = 0; n < 40; n++) {
    if ((await target.evaluate(() => Boolean(document.fullscreenElement))) === entered)
      return true;
    await target.waitForTimeout(50);
  }
  return false;
};
// Zoom must enlarge the drawn plate, not just widen its box; Fit restores
// the whole plate.
const plateScale = (target) =>
  target.locator('.frame-art').evaluate((image) => {
    const box = image.getBoundingClientRect();
    return Math.min(box.width / image.naturalWidth, box.height / image.naturalHeight);
  });
const zoomEnlarges = async (target) => {
  await openMore(target);
  const fit = await plateScale(target);
  await target.getByRole('button', { name: 'Zoom', exact: true }).click();
  const zoomed = await plateScale(target);
  await layout(target, zoomed > fit * 1.5, 'Zoom does not enlarge the plate', { fit, zoomed });
  await target.getByRole('button', { name: 'Fit whole plate', exact: true }).click();
  const restored = await plateScale(target);
  await layout(target, Math.abs(restored - fit) < 0.001, 'Fit does not restore the plate', { fit, restored });
};
// With More closed and the page unscrolled, the control bar sits fully
// below the bird name and the owner's note under it, and every control is
// a full-size tap target.
const captionClear = async (target) => {
  await target.evaluate(() => scrollTo(0, 0));
  await target.keyboard.press('Shift');
  await target.locator('.frame-controls:not(.quiet)').waitFor();
  assert.equal(await target.locator('.frame-controls.expanded').count(), 0);
  const note = target.locator('.frame-side > p.hint', { hasText: 'Plates change once a minute' });
  const found = await boxes({
    caption: target.locator('.frame-caption'),
    bar: target.locator('.frame-controls'),
    ...((await note.count()) ? { note } : {}),
  });
  // Half-pixel tolerance for subpixel layout.
  const above = (box) => box && found.bar && box.y + box.height <= found.bar.y + 0.5;
  await layout(target, above(found.caption), 'the bar covers the bird name', found);
  if ('note' in found) await layout(target, above(found.note), 'the bar covers the note', found);
  for (const control of await target.locator('.frame-controls').locator('button:visible, a:visible').all()) {
    const box = await control.boundingBox();
    await layout(target, box && box.width >= 44 && box.height >= 44, 'a Frame control is under 44 px', {
      control: box,
      name: await control.innerText(),
    });
  }
};
// The small phone sizes the bar must also clear.
const captionClearSmall = async (target) => {
  for (const viewport of [{ width: 320, height: 568 }, { width: 375, height: 667 }]) {
    await target.setViewportSize(viewport);
    await captionClear(target);
  }
  await target.setViewportSize({ width: 390, height: 844 });
};
// Another system's fonts: a wider fallback face and taller lines. No Frame
// layout may depend on how tall its text is.
const STRESSED_TEXT =
  '* { font-family: "Courier New", monospace !important; letter-spacing: .04em !important; line-height: 1.6 !important }';
// Boxes once they stop moving, so a measurement never races a layout pass.
async function settledBoxes(target, named) {
  let last = JSON.stringify(await boxes(named));
  for (let n = 0; n < 20; n++) {
    await target.waitForTimeout(50);
    const now = JSON.stringify(await boxes(named));
    if (now === last) break;
    last = now;
  }
  return JSON.parse(last);
}
// Fills the notice with as much of its own text, repeated, as fits in
// exactly `lines` lines, and returns the line count it shows.
const noticeLines = (notice, words, lines) =>
  notice.evaluate(
    (status, [words, lines]) => {
      const count = () => {
        const range = document.createRange();
        range.selectNodeContents(status);
        return new Set([...range.getClientRects()].map((line) => Math.round(line.top))).size;
      };
      let text = '';
      for (let n = 0; n < 400; n++) {
        const longer = (text ? text + ' ' : '') + words[n % words.length];
        status.textContent = longer;
        if (count() > lines) break;
        text = longer;
      }
      status.textContent = text;
      return count();
    },
    [words, lines],
  );
// One-, two- and three-line notices in Frame never cover the bird name or
// the note, and sit above the bar, at every size.
async function noticeClear(t, stressed) {
  const { page, origin, plateRoute } = t;
  const label = stressed ? 'stress-' : '';
  // The page's own stylesheet carries the stress rules: its policy refuses
  // inline styles.
  const stylesheet = /\/app\/app\.css(\?|$)/;
  const stress = async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, body: (await response.text()) + '\n' + STRESSED_TEXT });
  };
  if (stressed) await page.route(stylesheet, stress);
  for (const [width, height] of [
    [320, 568],
    [375, 667],
    [375, 812],
    [844, 390],
    [1280, 800],
  ]) {
    const size = width + 'x' + height;
    t.phase = 'frame-notice-clear-' + label + size;
    await page.setViewportSize({ width, height });
    await page.goto(origin + '/app?page=frame&view=all');
    await page.locator('.frame-art').waitFor();
    await captionClear(page);
    await page.route(plateRoute, (route) => route.abort());
    await page.keyboard.press('Shift');
    await page.locator('.frame-controls:not(.quiet)').waitFor();
    await page.getByRole('button', { name: 'Next', exact: true }).click();
    await page.getByText(/This plate did not load\. Keeping the last one/).waitFor();
    const notice = page.locator('.frame-dock [role="status"]');
    const words = (await notice.innerText()).split(/\s+/);
    for (const lines of [1, 2, 3]) {
      const shown = await noticeLines(notice, words, lines);
      await page.evaluate(() => scrollTo(0, 0));
      const found = await settledBoxes(page, {
        notice,
        name: page.locator('.frame-caption'),
        note: page.locator('.frame-side > p.hint', { hasText: 'Plates change once a minute' }),
        bar: page.locator('.frame-controls'),
      });
      const where = size + ', ' + lines + '-line notice: ';
      await layout(page, shown === lines, where + 'the notice shows ' + shown + ' lines', found);
      const apart = (other) =>
        found.notice &&
        other &&
        (other.y + other.height <= found.notice.y + 0.5 ||
          found.notice.y + found.notice.height <= other.y + 0.5);
      await layout(page, apart(found.name), where + 'the notice covers the bird name', found);
      await layout(page, apart(found.note), where + 'the notice covers the note', found);
      await layout(
        page,
        found.notice && found.bar && found.notice.y + found.notice.height <= found.bar.y + 0.5,
        where + 'the notice is not above the bar',
        found,
      );
      await layout(
        page,
        found.bar && found.bar.y + found.bar.height <= height + 0.5,
        where + 'the bar is below the window',
        found,
      );
      for (const key of ['name', 'note'])
        await layout(
          page,
          found.bar && found[key] && found[key].y + found[key].height <= found.bar.y + 0.5,
          where + 'the bar covers the ' + (key === 'name' ? 'bird name' : 'note'),
          found,
        );
    }
    await page.unroute(plateRoute);
  }
  if (stressed) await page.unroute(stylesheet, stress);
}

// Paired read-only screen; its context stays open for the integrations phase.
async function pairedScreen(t) {
  const { page, context, browser, origin, fixture, root, engine, heading, cardinal, robin, publicCatalog, errors, trip } = t;
  t.phase = 'pair-readonly-screen';
  await page.goto(origin + '/app?page=displays');
  await heading('Wall frame');
  const screenContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: engine !== 'firefox',
    hasTouch: engine !== 'firefox',
    reducedMotion: 'reduce',
  });
  await screenContext.route('**/*', (route) =>
    new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
  );
  const screen = await screenContext.newPage();
  await screen.clock.install();
  screen.on('pageerror', (error) => errors.push({ phase: t.phase, message: error.message }));
  await screen.goto(origin + '/screen');
  await screen.getByRole('button', { name: 'Pair this screen', exact: true }).click();
  await screen.getByRole('heading', { name: 'Pair this screen', exact: true }).waitFor();
  const pairCode = await screen.locator('.pairing-code').innerText();
  // A failed Cancel keeps the code, and its message survives the next
  // approval check instead of being replaced within seconds.
  await screenContext.route('**/v1/screen/forget', (route) => route.abort());
  await screen.getByRole('button', { name: 'Cancel pairing', exact: true }).click();
  const cancelFailure = screen.getByText(/Pairing was not cancelled\./);
  await cancelFailure.waitFor();
  const approvalCheck = screen.waitForResponse((response) =>
    response.url().endsWith('/v1/screen/view'),
  );
  await screen.clock.runFor(15100);
  await approvalCheck;
  await screen.getByText(/Waiting for owner approval\./).waitFor();
  assert(await cancelFailure.isVisible(), 'the Cancel failure stays visible');
  assert.equal(await screen.locator('.pairing-code').innerText(), pairCode);
  await screenContext.unroute('**/v1/screen/forget');
  await page.reload();
  await page.getByRole('heading', { name: 'Screen waiting to pair · ' + pairCode, exact: true }).waitFor();
  await page.getByLabel('Screen name', { exact: true }).fill('Synthetic tablet');
  await page.getByLabel('Permitted view', { exact: true }).first().selectOption(trip.collection_id);
  await page
    .getByLabel('I matched this code on the screen I want to pair', { exact: true })
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Pair this screen', exact: true }).click(),
  ]);
  await screen.clock.runFor(15100);
  await screen.locator('.frame-art').waitFor();
  await screen.locator('.frame-art').evaluate((image) => image.decode());
  await captionClear(screen);
  await captionClearSmall(screen);
  await zoomEnlarges(screen);
  await screen.keyboard.press('Escape');
  await screen.setViewportSize({ width: 844, height: 390 });
  await captionClear(screen);
  await screen.setViewportSize({ width: 390, height: 844 });
  assert.equal((await screenContext.request.get(origin + '/v1/owner/journal')).status(), 401);
  const screenView = (await (await screenContext.request.get(origin + '/v1/screen/view')).json())
    .data;
  assert.equal(screenView.species.length, 1);
  assert.equal(screenView.species[0].taxon_id, cardinal.taxon_id);
  t.phase = 'authoritative-screen-scope-removal';
  const replacement = publicCatalog.find(
    (bird) => bird.taxon_id !== cardinal.taxon_id && bird.taxon_id !== robin.taxon_id,
  );
  fs.cpSync(
    path.join(root, 'catalog', path.dirname(replacement.portrait_path)),
    path.join(fixture, 'catalog', path.dirname(replacement.portrait_path)),
    { recursive: true },
  );
  const catalogFile = path.join(fixture, 'catalog/index.json'),
    expanded = JSON.parse(fs.readFileSync(catalogFile));
  expanded.species.push(replacement);
  fs.writeFileSync(catalogFile, JSON.stringify(expanded));
  const ownerSession = (await (await context.request.get(origin + '/v1/owner/session')).json())
    .data;
  let currentJournal = (await (await context.request.get(origin + '/v1/owner/journal')).json())
    .data;
  // Where the engine honors a trusted fullscreen request, the paired screen
  // runs the following state changes as an ambient display, and each notice
  // must land inside the live window rather than below the full-height plate.
  let screenAmbient = false;
  await openMore(screen);
  const screenFullscreen = screen.getByRole('button', { name: 'Fullscreen', exact: true });
  if ((await screenFullscreen.count()) === 1) {
    await screenFullscreen.click();
    await fullscreenSettles(screen, true);
    screenAmbient = (await screen.evaluate(() => document.fullscreenElement?.tagName)) === 'HTML';
  }
  const screenInside = async (locator, what) => {
    const size = await screen.evaluate(() => ({ width: innerWidth, height: innerHeight }));
    const box = await locator.boundingBox();
    await layout(
      screen,
      box && box.y >= 0 && box.x >= 0 && box.y + box.height <= size.height && box.x + box.width <= size.width,
      what + ' is outside the window',
      { box },
    );
  };
  await screenContext.route('**/v1/screen/plates/' + replacement.taxon_id + '?*', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: '{"ok":false}' }),
  );
  assert.equal(
    (
      await context.request.post(origin + '/v1/owner/journal/collections/save', {
        headers: { Origin: origin, 'X-Inky-CSRF': ownerSession.csrf },
        data: {
          revision: currentJournal.revision,
          collection_id: trip.collection_id,
          label: trip.label,
          taxon_ids: [replacement.taxon_id],
        },
      })
    ).status(),
    200,
  );
  await screen.clock.runFor(15100);
  await screen.getByText(/not available right now/).waitFor();
  if (screenAmbient) await screenInside(screen.getByText(/not available right now/), 'the plate notice');
  assert.equal(await screen.locator('img[src]').count(), 0);
  currentJournal = (await (await context.request.get(origin + '/v1/owner/journal')).json()).data;
  assert.equal(
    (
      await context.request.post(origin + '/v1/owner/journal/collections/remove', {
        headers: { Origin: origin, 'X-Inky-CSRF': ownerSession.csrf },
        data: { revision: currentJournal.revision, collection_id: trip.collection_id },
      })
    ).status(),
    200,
  );
  await screen.clock.runFor(15100);
  const emptyNotice = screen.getByText(
    'This view has no plates now. The screen waits here and starts again when it has some.',
    { exact: true },
  );
  await emptyNotice.waitFor();
  if (screenAmbient) await screenInside(emptyNotice, 'the empty-view notice');
  assert.equal(await screen.locator('img[src]').count(), 0);
  // An emptied view leaves nothing to step through.
  await screen.keyboard.press('ArrowRight');
  assert.equal(await screen.locator('img[src]').count(), 0);
  t.phase = 'screen-forget-failure';
  // Forget asks first, and a failed request keeps the screen paired and
  // presenting instead of clearing it before the answer.
  await screenContext.route('**/v1/screen/forget', (route) => route.abort());
  await openMore(screen);
  await screen.getByRole('button', { name: 'Forget this screen', exact: true }).click();
  await screen.getByRole('button', { name: 'Forget', exact: true }).click();
  await screen.getByText(/This screen was not forgotten\./).waitFor();
  assert(await screen.evaluate(() => document.body.classList.contains('presenting')));
  await screen.getByRole('button', { name: 'Forget this screen', exact: true }).waitFor();
  await screenContext.unroute('**/v1/screen/forget');
  // Revoking asks first; the first press changes nothing.
  await page.getByRole('button', { name: 'Revoke screen', exact: true }).click();
  await page.getByRole('button', { name: 'Revoke', exact: true }).click();
  await screen.clock.runFor(15100);
  await screen.getByRole('heading', { name: 'Screen access ended', exact: true }).waitFor();
  assert.equal(await screen.locator('img').count(), 0);
  Object.assign(t, { screenContext, replacement });
}

async function ownerFrame(t) {
  const { page, origin, engine, plateRoute, imported } = t;
  t.phase = 'owner-frame';
  await page.goto(origin + '/app?page=frame&view=' + imported.collection_id);
  await page.locator('.frame-art').waitFor();
  await page.locator('.frame-art').evaluate((image) => image.decode());
  await zoomEnlarges(page);
  await noSideways(page, 'Frame');
  t.phase = 'frame-ambient-labels';
  // Toggles keep their names and report state through aria-pressed.
  const labels = page.getByRole('button', { name: 'Bird name', exact: true });
  assert.equal(await labels.getAttribute('aria-pressed'), 'true');
  await page.locator('.frame-caption').waitFor();
  await labels.click();
  assert.equal(await labels.getAttribute('aria-pressed'), 'false');
  assert(!(await page.locator('.frame-caption').isVisible()));
  await labels.click();
  assert.equal(await labels.getAttribute('aria-pressed'), 'true');
  await page.locator('.frame-caption').waitFor();
  for (const name of ['Play', 'Shuffle'])
    assert.equal(
      await page.getByRole('button', { name, exact: true }).getAttribute('aria-pressed'),
      'false',
    );
  t.phase = 'frame-ambient-fade';
  // Idle fades the controls even though the clicked toggle may still hold
  // focus; keyboard activity brings them back.
  await page.clock.runFor(5100);
  await page.locator('.frame-controls.quiet').waitFor();
  assert.equal(
    await page.getByRole('button', { name: 'More', exact: true }).getAttribute('aria-expanded'),
    'false',
  );
  assert(await page.locator('.frame-controls').evaluate((bar) => getComputedStyle(bar).pointerEvents === 'none'));
  if (engine !== 'firefox') {
    // A tap on faded controls only wakes them: it must not also press Play.
    const playBox = await page.getByRole('button', { name: 'Play', exact: true }).boundingBox();
    await page.touchscreen.tap(playBox.x + playBox.width / 2, playBox.y + playBox.height / 2);
    await page.locator('.frame-controls:not(.quiet)').waitFor();
    assert.equal(
      await page.getByRole('button', { name: 'Play', exact: true }).getAttribute('aria-pressed'),
      'false',
    );
    await page.clock.runFor(5100);
    await page.locator('.frame-controls.quiet').waitFor();
  }
  await page.keyboard.press('Shift');
  await page.locator('.frame-controls:not(.quiet)').waitFor();
  // Ambient fullscreen, where the engine honors a trusted request: the plate
  // owns the viewport, the placard stays inside it, a cleared caption leaves
  // no empty box, and failure notices stay on screen rather than below it.
  t.phase = 'frame-ambient-fullscreen';
  // The all-artwork view always holds at least two ready plates here, so
  // Next must fetch a new portrait and the aborted request must surface its
  // notice. The Fullscreen button renders only where the API exists, and an
  // engine may still refuse the request; both gates skip without weakening
  // the journeys that do enter real fullscreen.
  await page.goto(origin + '/app?page=frame&view=all');
  await page.locator('.frame-art').waitFor();
  await page.locator('.frame-art').evaluate((image) => image.decode());
  await openMore(page);
  const fullscreenButton = page.getByRole('button', { name: 'Fullscreen', exact: true });
  if ((await fullscreenButton.count()) === 1) {
    await fullscreenButton.click();
    await fullscreenSettles(page, true);
  }
  if ((await page.evaluate(() => document.fullscreenElement?.tagName)) === 'HTML') {
    // Fullscreen can resize the window to the real screen (Firefox), so the
    // live window metrics are the frame of reference, not the viewport.
    const size = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
    const inside = async (locator, what) => {
      const box = await locator.boundingBox();
      await layout(
        page,
        box && box.y >= 0 && box.x >= 0 && box.y + box.height <= size.height && box.x + box.width <= size.width,
        what + ' is outside the fullscreen window',
        { box, window: size },
      );
    };
    await inside(page.locator('.frame-caption'), 'the bird name');
    assert(
      await page.evaluate(() => {
        const caption = document.querySelector('.frame-caption');
        const kept = caption.textContent;
        caption.textContent = '';
        const hidden = getComputedStyle(caption).display === 'none';
        caption.textContent = kept;
        return hidden;
      }),
    );
    await page.route(plateRoute, (route) => route.abort());
    await page.keyboard.press('Shift');
    await page.getByRole('button', { name: 'Next', exact: true }).click();
    const notice = page.getByText(/This plate did not load\. Keeping the last one/);
    await notice.waitFor();
    await inside(notice, 'the notice');
    await page.unroute(plateRoute);
    await page.evaluate(() => document.exitFullscreen());
    assert(await fullscreenSettles(page, false));
  }
  t.phase = 'frame-ambient-notice-clearance';
  // On every engine, at phone width with the controls revealed, a populated
  // notice must sit fully above the one-row control bar, never behind it.
  await page.keyboard.press('Shift');
  await page.locator('.frame-controls:not(.quiet)').waitFor();
  await page.route(plateRoute, (route) => route.abort());
  // At phone width the bar never scrolls sideways: previous, play, next and
  // More are always on screen, and every control behind More fits the
  // window when it opens. Previous then steps to the other plate, whose
  // aborted fetch surfaces the notice for the clearance geometry.
  const windowWidth = await page.evaluate(() => innerWidth);
  const fits = async (locator) => {
    const box = await locator.boundingBox();
    // One-pixel tolerance for subpixel layout.
    await layout(
      page,
      box && box.x >= -1 && box.x + box.width <= windowWidth + 1,
      'a Frame control is outside the window',
      { control: box, name: await locator.innerText() },
    );
  };
  const barFits = async () => {
    const bar = await page
      .locator('.frame-controls')
      .evaluate((node) => ({ scrollWidth: node.scrollWidth, clientWidth: node.clientWidth }));
    await layout(page, bar.scrollWidth <= bar.clientWidth + 1, 'the bar scrolls sideways', { bar });
  };
  await barFits();
  for (const name of ['Previous', 'Play', 'Next', 'More'])
    await fits(page.getByRole('button', { name, exact: true }));
  await openMore(page);
  await barFits();
  for (const control of await page.locator('#frame-more').locator('button:visible, a:visible').all())
    await fits(control);
  await page.keyboard.press('Escape');
  assert.equal(
    await page.getByRole('button', { name: 'More', exact: true }).getAttribute('aria-expanded'),
    'false',
  );
  await captionClear(page);
  await captionClearSmall(page);
  const previousControl = page.getByRole('button', { name: 'Previous', exact: true });
  await previousControl.click();
  const clearanceNotice = page.getByText(/This plate did not load\. Keeping the last one/);
  await clearanceNotice.waitFor();
  const clearance = await boxes({ notice: clearanceNotice, bar: page.locator('.frame-controls') });
  await layout(
    page,
    clearance.notice &&
      clearance.bar &&
      clearance.notice.y >= 0 &&
      clearance.notice.y + clearance.notice.height <= clearance.bar.y,
    'the notice is not above the bar',
    clearance,
  );
  await page.unroute(plateRoute);
  // A notice never covers the bird name or the note under the plate, on a
  // small phone, a phone or a desktop, with system fonts and with stressed
  // text metrics.
  const restoreSize = page.viewportSize();
  await noticeClear(t, false);
  await noticeClear(t, true);
  await page.setViewportSize(restoreSize);
  await page.goto(origin + '/app?page=frame&view=all');
  await page.locator('.frame-art').waitFor();
  await page.locator('.frame-art').evaluate((image) => image.decode());
  t.phase = 'frame-swipe';
  // A horizontal mouse drag on the plate steps to the next plate instead of
  // starting a native image drag.
  const swipeCaption = await page.locator('.frame-caption').innerText();
  const plateBox = await page.locator('.frame-viewport').boundingBox();
  await page.mouse.move(plateBox.x + plateBox.width * 0.75, plateBox.y + plateBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(plateBox.x + plateBox.width * 0.25, plateBox.y + plateBox.height / 2, {
    steps: 5,
  });
  await page.mouse.up();
  await page.waitForFunction(
    (before) => document.querySelector('.frame-caption')?.innerText !== before,
    swipeCaption,
  );
  if (engine === 'chromium') {
    // A finger swipe on the plate while the controls are faded steps too:
    // the touch that wakes the bar must not swallow the step.
    await page.clock.runFor(5100);
    await page.locator('.frame-controls.quiet').waitFor();
    const touchCaption = await page.locator('.frame-caption').innerText();
    const y = plateBox.y + plateBox.height / 2;
    const touch = await page.context().newCDPSession(page);
    const finger = (type, x) =>
      touch.send('Input.dispatchTouchEvent', {
        type,
        touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }],
      });
    await finger('touchStart', plateBox.x + plateBox.width * 0.8);
    for (const share of [0.65, 0.5, 0.35, 0.2])
      await finger('touchMove', plateBox.x + plateBox.width * share);
    await finger('touchEnd');
    await touch.detach();
    await page.waitForFunction(
      (before) => document.querySelector('.frame-caption')?.innerText !== before,
      touchCaption,
    );
  }
  t.phase = 'frame-resilience';
  // A network outage fails both the periodic check and the next plate, yet
  // playback continues; an emptied view recovers without a reload once its
  // plates return.
  await page.keyboard.press('Shift');
  const playControl = page.getByRole('button', { name: 'Play', exact: true });
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  await page.route('**/v1/owner/journal', (route) => route.abort());
  await page.route(plateRoute, (route) => route.abort());
  await page.clock.runFor(60100);
  await page.getByText(/Keeping the last plate; Inky keeps checking\./).waitFor();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  await page.unroute('**/v1/owner/journal');
  await page.route('**/v1/owner/journal', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const bird of body.data.birds) bird.plate_ready = false;
    await route.fulfill({ response, json: body });
  });
  await page.clock.runFor(60100);
  await page.getByText(/This view has no plates now\./).waitFor();
  assert.equal(await page.locator('.frame-art[src]').count(), 0);
  // Playback stays on, but an emptied view has nothing left to step to.
  const emptiedSteps = [];
  const countStep = (request) => {
    if (/\/v1\/owner\/journal\/birds\/\d+\/portrait\?revision=/.test(request.url()))
      emptiedSteps.push(request);
  };
  page.on('request', countStep);
  await page.keyboard.press('ArrowRight');
  await page.clock.runFor(120100);
  await page.waitForTimeout(100);
  page.off('request', countStep);
  assert.equal(emptiedSteps.length, 0, 'an emptied view stops stepping');
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('.frame-art[src]').count(), 0);
  await page.unroute('**/v1/owner/journal');
  await page.unroute(plateRoute);
  await page.clock.runFor(60100);
  await page.locator('.frame-art[src]').waitFor();
  await page.keyboard.press('Shift');
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'false');
  t.phase = 'frame-broken-plate';
  // One plate that keeps failing must not hold playback on one bird. A
  // synthetic copy of a ready plate under another id makes three plates, so
  // the step past the broken one shows a different bird. The plate after the
  // shown one fails; the one after that must appear.
  const shownId = Number(
    new URL(
      await page.locator('.frame-more a[href*="page=bird"]').getAttribute('href'),
      origin,
    ).searchParams.get('bird'),
  );
  const copyId = 990001;
  const plates = { broken: null, source: null, expected: null };
  await page.route('**/v1/owner/journal', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const ready = body.data.birds.filter((bird) => bird.plate_ready);
    plates.source = ready[0].taxon_id;
    ready.push({
      ...ready[0],
      taxon_id: copyId,
      common_name: 'Synthetic copy plate',
      portrait_url: '/v1/owner/journal/birds/' + copyId + '/portrait',
    });
    body.data.birds.push(ready.at(-1));
    const at = ready.findIndex((bird) => bird.taxon_id === shownId);
    plates.broken = ready[(at + 1) % ready.length].taxon_id;
    plates.expected = ready[(at + 2) % ready.length].common_name;
    await route.fulfill({ response, json: body });
  });
  await page.route(plateRoute, async (route) => {
    const id = Number(route.request().url().split('/birds/')[1].split('/')[0]);
    if (id === plates.broken) return route.abort();
    if (id !== copyId) return route.continue();
    const original = route.request().url().replace('/birds/' + copyId + '/', '/birds/' + plates.source + '/');
    return route.fulfill({ response: await route.fetch({ url: original }) });
  });
  // While paused, one check picks up the third plate.
  const thirdPlate = page.waitForResponse((response) =>
    response.url().endsWith('/v1/owner/journal'),
  );
  await page.clock.runFor(60100);
  await thirdPlate;
  assert(plates.broken !== null && plates.expected !== null);
  await page.keyboard.press('Shift');
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  const shownName = () => page.locator('.frame-caption span').first().innerText();
  // Small steps let each plate request settle before the next timer fires.
  for (let waited = 0; waited < 240000 && (await shownName()) !== plates.expected; waited += 500) {
    await page.clock.runFor(500);
    await page.waitForTimeout(20);
  }
  assert.equal(await shownName(), plates.expected, 'playback moves past a plate that keeps failing');
  await page.keyboard.press('Shift');
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'false');
  await page.unroute(plateRoute);
  await page.unroute('**/v1/owner/journal');
  t.phase = 'frame-wide';
  // Wide screens show every control in one bar and no More button.
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.keyboard.press('Shift');
  await page.locator('.frame-controls:not(.quiet)').waitFor();
  assert.equal(await page.getByRole('button', { name: 'More', exact: true }).count(), 0);
  for (const name of ['Previous', 'Play', 'Next', 'Shuffle', 'Bird name', 'Zoom'])
    assert(await page.getByRole('button', { name, exact: true }).isVisible());
  const wideBar = await page.locator('.frame-controls').evaluate((node) => ({
    scrollWidth: node.scrollWidth,
    clientWidth: node.clientWidth,
    ...node.getBoundingClientRect().toJSON(),
  }));
  await layout(page, wideBar.scrollWidth <= wideBar.clientWidth + 1, 'the wide bar scrolls sideways', { bar: wideBar });
  await layout(page, wideBar.bottom <= 800, 'the wide bar is below the window', { bar: wideBar });
  await captionClear(page);
  await zoomEnlarges(page);
  await page.setViewportSize({ width: 844, height: 390 });
  await captionClear(page);
  await page.setViewportSize({ width: 390, height: 844 });
  t.phase = 'frame-plain-http';
  // Over plain HTTP the page adds a note that the screen may sleep; the bar
  // must still clear it, the bird name and the other note at every size.
  const plain = await page.context().newPage();
  try {
    await plain.addInitScript(() =>
      Object.defineProperty(window, 'isSecureContext', { configurable: true, get: () => false }),
    );
    await plain.goto(origin + '/app?page=frame&view=' + imported.collection_id);
    await plain.locator('.frame-art').waitFor();
    const sleepNote = plain.locator('.frame-side > p.hint', { hasText: 'This screen may sleep' });
    await sleepNote.waitFor();
    for (const viewport of [
      { width: 320, height: 568 },
      { width: 375, height: 812 },
      { width: 1280, height: 800 },
    ]) {
      await plain.setViewportSize(viewport);
      await captionClear(plain);
      const found = await boxes({ sleepNote, bar: plain.locator('.frame-controls') });
      await layout(
        plain,
        found.sleepNote && found.bar && found.sleepNote.y + found.sleepNote.height <= found.bar.y + 0.5,
        'the bar covers the sleep note',
        found,
      );
      await noSideways(plain, 'Frame over plain HTTP');
    }
  } finally {
    await plain.close();
  }
}

module.exports = { pairedScreen, ownerFrame };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
