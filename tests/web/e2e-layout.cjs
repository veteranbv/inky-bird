// Layout assertions for the journal browser journeys. A failure names the
// window and every measured box, so a CI log alone explains it.
const assert = require('node:assert/strict');

const rounded = (value) =>
  value && typeof value === 'object'
    ? Object.fromEntries(
        Object.entries(value).map(([key, item]) => [
          key,
          typeof item === 'number' ? Math.round(item * 10) / 10 : rounded(item),
        ]),
      )
    : value;

const windowSize = (page) =>
  page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    scrollX,
    scrollY,
    scrollWidth: document.documentElement.scrollWidth,
    fullscreen: Boolean(document.fullscreenElement),
  }));

// Fails with `what`, the window and `boxes` unless `ok`.
async function layout(page, ok, what, boxes = {}) {
  if (ok) return;
  const viewport = await windowSize(page).catch(() => null);
  assert.fail(what + ' ' + JSON.stringify(rounded({ viewport, ...boxes })));
}

// The page never scrolls sideways.
async function noSideways(page, what) {
  const size = await windowSize(page);
  await layout(page, size.scrollWidth <= size.width, what + ': the page scrolls sideways');
}

// Element boxes in the window's coordinates; null for an element not laid out.
const boxes = async (named) =>
  Object.fromEntries(
    await Promise.all(
      Object.entries(named).map(async ([name, locator]) => [name, await locator.boundingBox()]),
    ),
  );

module.exports = { layout, noSideways, boxes };

if (require.main === module) {
  console.error('Run tests/web/journal.e2e.cjs');
  process.exitCode = 2;
}
