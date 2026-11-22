// Record the demo film's app scenes as separate clips from the real app.
// Each scene is a fresh 1920x1080 recording context against one synthetic
// fixture: disposable owner, five public approved plates, a Garden birds
// collection made through the real UI, generation off, every non-loopback
// request aborted and logged. Account creation happens before any recording;
// the recording contexts only receive the in-memory storage state.
//
//   node tests/web/capture-demo.cjs <output-dir>
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { mkdirSync, writeFileSync, renameSync } = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.INKY_QA_PLAYWRIGHT || 'playwright');
const { startFixture } = require('./demo-fixture.cjs');

const PLATES = [9083, 12727, 12942, 8229, 145310];
const COLLECTION_BIRDS = ['Northern Cardinal', 'American Robin', 'Eastern Bluebird', 'American Goldfinch'];

const argv = process.argv.slice(2);
const output = path.resolve(argv[0] || '');
assert(argv.length === 1, 'Usage: node tests/web/capture-demo.cjs <output-dir>');
mkdirSync(output, { recursive: true });

const fixture = startFixture(PLATES);
const viewport = { width: 1920, height: 1080 };

// A visible cursor: headless Chromium draws none, so every recorded click is
// preceded by this fake cursor gliding to the target, with a click ripple.
const CURSOR = `
(() => {
  if (window !== window.top) return;
  const make = () => {
    if (document.getElementById('demo-cursor')) return;
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    cursor.style.cssText = 'position:fixed;left:0;top:0;width:26px;height:26px;' +
      'margin:-13px 0 0 -13px;border-radius:50%;pointer-events:none;z-index:2147483647;' +
      'background:rgba(29,48,41,.28);border:2.5px solid rgba(29,48,41,.85);' +
      'box-shadow:0 2px 10px rgba(0,0,0,.3), inset 0 0 0 2px rgba(255,253,245,.7);' +
      'transform:translate(960px,620px);transition:transform .66s cubic-bezier(.3,.1,.25,1)';
    document.body.append(cursor);
    window.__demoCursor = {
      moveTo: (x, y) => new Promise((resolve) => {
        const done = () => { cursor.removeEventListener('transitionend', done); resolve(); };
        cursor.addEventListener('transitionend', done);
        setTimeout(done, 760);
        cursor.style.transform = 'translate(' + x + 'px,' + y + 'px)';
      }),
      ripple: () => new Promise((resolve) => {
        const ring = document.createElement('div');
        ring.style.cssText = cursor.style.cssText +
          ';transition:transform .45s ease-out,opacity .45s ease-out;opacity:.9;background:transparent';
        ring.style.transform = cursor.style.transform;
        document.body.append(ring);
        requestAnimationFrame(() => {
          ring.style.transform = cursor.style.transform + ' scale(2.4)';
          ring.style.opacity = '0';
        });
        setTimeout(() => { ring.remove(); resolve(); }, 470);
      }),
    };
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', make);
  else make();
})();`;

async function record() {
  const origin = await fixture.origin();
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-background-networking', '--use-mock-keychain'],
  });
  try {
    // Everything before this point in app state happens without a recording:
    // the password exists only in this process and the fixture's own store.
    const setup = await browser.newContext({
      viewport, deviceScaleFactor: 1,
      reducedMotion: 'reduce', colorScheme: 'light', serviceWorkers: 'block',
    });
    await setup.route('**/*', (route) =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await setup.newPage();
    const setupErrors = [];
    page.on('pageerror', (error) => setupErrors.push(error.message));
    const password = randomBytes(32).toString('base64url');
    await page.goto(origin + '/app');
    await page.getByLabel('Username', { exact: true }).fill('demo-owner');
    await page.getByLabel('Choose your Inky password', { exact: true }).fill(password);
    await page.getByLabel('Confirm your password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await page.getByRole('heading', { name: 'Your journal', exact: true }).waitFor();
    await page.goto(origin + '/app?page=edit-collection');
    await page.getByLabel('Collection name', { exact: true }).fill('Garden birds');
    for (const bird of COLLECTION_BIRDS)
      await page.getByRole('checkbox', { name: new RegExp(bird) }).check();
    await page.getByRole('button', { name: 'Save collection', exact: true }).click();
    await page.getByRole('link', { name: 'Garden birds', exact: true }).waitFor();
    // Robin first, cardinal last: Play opens on the cardinal and Next moves
    // to the robin, exactly what the frame scene narrates.
    for (const taxon of [12727, 9083]) {
      await page.goto(origin + `/app?page=bird&bird=${taxon}`);
      await page.getByRole('button', { name: 'Add to my frame', exact: true }).click();
      await page.getByText('Added to On my frame.', { exact: true }).waitFor({ timeout: 30000 });
    }
    assert.deepEqual(setupErrors, []);
    const storageState = await setup.storageState();
    await setup.close();

    async function scene(id, body) {
      const context = await browser.newContext({
        viewport, deviceScaleFactor: 1, storageState,
        recordVideo: { dir: output, size: viewport },
        reducedMotion: 'reduce', colorScheme: 'light', serviceWorkers: 'block',
      });
      const blocked = [];
      await context.route('**/*', (route) => {
        if (new URL(route.request().url()).origin === origin) return route.continue();
        blocked.push(route.request().url());
        return route.abort();
      });
      await context.addInitScript(CURSOR);
      const film = await context.newPage();
      const errors = [];
      film.on('pageerror', (error) => errors.push(error.message));
      const started = performance.now();
      const events = [];
      let readyAt = null;
      const t = {
        page: film,
        origin,
        // Marks the first fully drawn moment; the composition trims to here.
        async ready() {
          await film.evaluate(async () => {
            await document.fonts.ready;
            for (const image of document.querySelectorAll('main img')) await image.decode().catch(() => {});
          });
          if (readyAt === null) readyAt = (performance.now() - started) / 1000;
        },
        async hold(seconds) {
          await film.waitForTimeout(seconds * 1000);
        },
        // Glides the fake cursor onto the target, ripples and really clicks.
        async act(name, locator, { click = true } = {}) {
          // Real pointer movement first: the Play view hides its toolbar when
          // the pointer goes quiet, and a hidden control cannot be clicked.
          await film.mouse.move(viewport.width / 2, viewport.height - 90);
          await film.mouse.move(viewport.width / 2 - 40, viewport.height - 60);
          await locator.scrollIntoViewIfNeeded();
          const box = await locator.boundingBox();
          assert(box, `No box for ${name}`);
          const [x, y] = [box.x + box.width / 2, box.y + box.height / 2];
          await film.evaluate(([cx, cy]) => window.__demoCursor.moveTo(cx, cy), [x, y]);
          await film.waitForTimeout(160);
          events.push({ seconds: (performance.now() - started) / 1000, action: name, selector: String(locator) });
          if (click) {
            await film.evaluate(() => window.__demoCursor.ripple());
            await locator.click();
          }
        },
        async type(name, locator, text) {
          await t.act(name, locator);
          await locator.pressSequentially(text, { delay: 85 });
          events.push({ seconds: (performance.now() - started) / 1000, action: `${name}: typed`, selector: String(locator) });
        },
      };
      await body(t);
      // A generous still tail so narration can stretch the scene hold.
      await t.hold(5);
      await film.screenshot({ path: path.join(output, `scene-${id}.png`) });
      const video = film.video();
      await context.close();
      const recorded = await video.path();
      renameSync(recorded, path.join(output, `${id}.webm`));
      assert(readyAt !== null, `Scene ${id} never marked ready`);
      writeFileSync(path.join(output, `${id}.events.json`), JSON.stringify({
        scene: id, viewport, deviceScaleFactor: 1, trimStart: readyAt,
        events, blockedExternalRequests: blocked, pageErrors: errors,
      }, null, 2) + '\n');
      assert.deepEqual(blocked, [], `Scene ${id} tried to leave loopback`);
      assert.deepEqual(errors, [], `Scene ${id} raised page errors`);
      console.log(`scene ${id}: ${((performance.now() - started) / 1000).toFixed(1)}s`);
    }

    const nav = (film, name) => film.getByRole('navigation', { name: 'Journal', exact: true })
      .getByRole('link', { name, exact: true });

    await scene('journal', async (t) => {
      await t.page.goto(origin + '/app');
      await t.page.getByRole('heading', { name: 'Your journal', exact: true }).waitFor();
      await t.ready();
      await t.hold(1.8);
      await t.act('Open Birds', nav(t.page, 'Birds'));
      await t.page.getByRole('heading', { name: 'Your birds', exact: true }).waitFor();
      await t.hold(0.4);
      await t.act('Show more plates', t.page.getByRole('navigation', { name: 'Bird scope', exact: true })
        .getByRole('link', { name: 'More plates', exact: true }));
      await t.page.getByRole('heading', { name: 'More plates', exact: true }).waitFor();
      await t.hold(0.3);
      await t.type('Find a bird', t.page.getByLabel('Find a bird', { exact: true }), 'cardinal');
      await t.page.getByText('1 species', { exact: true }).waitFor();
      await t.hold(0.6);
      await t.act('Open the Northern Cardinal page', t.page.getByRole('link', { name: /Northern Cardinal/ }));
      await t.page.getByRole('heading', { name: 'Northern Cardinal', exact: true }).waitFor();
      await t.ready();
      await t.hold(1.4);
      await t.act('Add to Favorites', t.page.getByRole('button', { name: 'Add to Favorites', exact: true }));
      await t.page.getByRole('button', { name: 'Remove from Favorites', exact: true }).waitFor();
    });

    await scene('collections', async (t) => {
      await t.page.goto(origin + '/app?page=collections');
      await t.page.getByRole('heading', { name: 'Collections', exact: true }).waitFor();
      await t.ready();
      await t.hold(1.6);
      await t.act('Open Garden birds', t.page.getByRole('link', { name: 'Garden birds', exact: true }));
      await t.page.getByRole('heading', { name: 'Garden birds', exact: true }).waitFor();
      await t.ready();
    });

    await scene('frame', async (t) => {
      await t.page.goto(origin + '/app?page=frame&view=current');
      await t.page.getByAltText('Plate: American Robin', { exact: true }).waitFor();
      await t.ready();
      await t.hold(1.2);
      await t.act('Press Play', t.page.getByRole('button', { name: 'Play', exact: true }));
      await t.hold(1.4);
      await t.act('Show the bird name', t.page.getByRole('button', { name: 'Bird name', exact: true }));
      await t.hold(1.4);
      await t.act('Zoom the plate', t.page.getByRole('button', { name: 'Zoom', exact: true }));
      await t.hold(1.8);
      await t.act('Fit the whole plate again', t.page.getByRole('button', { name: 'Fit whole plate', exact: true }));
      await t.hold(1);
      await t.act('Next plate', t.page.getByRole('button', { name: 'Next', exact: true }));
      await t.page.getByAltText('Plate: Northern Cardinal', { exact: true }).waitFor();
      await t.ready();
    });

    await scene('manage', async (t) => {
      await t.page.goto(origin + '/app?page=manage');
      await t.page.getByRole('heading', { name: 'Manage', exact: true }).waitFor();
      await t.ready();
      await t.hold(2);
      await t.act('Open Wall frame', t.page.getByRole('link', { name: /^Wall frame/ }));
      await t.page.getByRole('heading', { name: 'Wall frame', exact: true }).waitFor();
      await t.ready();
    });

    console.log(`Clips: ${output}`);
  } finally {
    await browser.close();
  }
}

record().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
}).finally(() => fixture.server.kill());
